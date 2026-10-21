#!/usr/bin/env python3
"""
Монитор новых статей The DFIR Report (https://thedfirreport.com/) —
автономная выгрузка для GitHub.

Проблема: из РФ сайт недоступен, поэтому статьи с <base href> открывались
«голыми» (картинки и стили не грузились). Решение:
  1. Картинки скачиваются на этапе крона и сохраняются в папку images/
     внутри папки отчёта; пути в HTML переписываются на локальные.
  2. Стили <link rel="stylesheet"> инлайнятся в <style>.
  3. <base> больше НЕ вставляется (ломал относительные пути картинок),
     <script> вырезается.
  4. Всё это — внутри GitHub Actions, где thedfirreport.com доступен,
     поэтому репозиторий становится самодостаточным и открывается из РФ.

Структура выгрузки:
    thedfirreport/
    ├── seen_dfir.db
    └── <отчёт>-<хэш>/
        ├── <отчёт>-<хэш>.html
        └── images/
            ├── <hash>.png
            └── ...

Фиксы, сохранённые из прошлой версии:
  - лента качается НАШИМ requests.Session (не голым feedparser);
  - VERIFY_SSL = False (корпоративный TLS-перехват);
  - нескачанные статьи НЕ помечаются обработанными.
"""

import hashlib
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import urllib3
from bs4 import BeautifulSoup

# ------------------------------------------------------- настройки
FEED_URL = "https://thedfirreport.com/feed/"
OUT_DIR  = Path("thedfirreport")     # корень: в нём папки отчётов + БД
DB_PATH  = Path("thedfirreport/seen_dfir.db")

# False - НЕ проверять TLS: корпоративный прокси/Zscaler подменяет сертификаты.
VERIFY_SSL = False

# True = вырезать <script> из сохранённого HTML.
STRIP_SCRIPTS = True

# True = скачивать картинки в локальную папку и переписывать пути.
DOWNLOAD_IMAGES = True

# True = скачивать CSS и встраивать в <style> (офлайн-стилизация).
INLINE_STYLES = True

# ВАЖНО: только ASCII! Русский текст в HTTP-заголовках запрещён стандартом.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 DfirMonitor/2.0"
)

EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://thedfirreport.com/",
}

TIMEOUT = 30
RETRIES = 2
REQUEST_DELAY = 1.0

# в каком порядке выбираем реальный src у картинок WP (srcset/lazy-атрибуты режем)
IMAGE_SRC_ATTRS = ("data-src", "src", "data-lazy-src", "data-orig-file")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("monitor")

if VERIFY_SSL is False:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ------------------------------------------------------- хранилище
def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            guid       TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            title      TEXT,
            published  TEXT,
            file_path  TEXT,
            first_seen TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, guid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM reports WHERE guid = ?", (guid,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, guid, url, title, published, file_path) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO reports (guid, url, title, published, file_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (guid, url, title, published, str(file_path)),
    )
    conn.commit()


# ------------------------------------------------------- утилиты
def normalize_url(url: str) -> str:
    url = url.strip()
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    return url.rstrip("/")


def entry_guid(entry) -> str:
    for key in ("id", "guid"):
        val = entry.get(key)
        if val:
            return str(val)
    return normalize_url(entry.get("link", "")) or entry.get("title", "")


def make_stem(url: str) -> str:
    """'.../some-report' -> 'some-report-1a2b3c4d'."""
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.UNICODE)
    if not slug:
        slug = "report"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def origin_of(url: str) -> str | None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def guess_ext(url: str) -> str:
    """Расширение файла по URL, иначе .bin."""
    path = urlparse(url).path.lower()
    m = re.search(
        r"\.(jpe?g|png|gif|webp|avif|svg|bmp|ico|pdf|woff2?|ttf|otf|eot)$",
        path,
    )
    return m.group(1) if m else ".bin"


# ------------------------------------------------------- сеть
def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.headers.update(EXTRA_HEADERS)

    adapter = requests.adapters.HTTPAdapter(max_retries=RETRIES)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_feed(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()
    return resp.content


# ------------------------------------------------------- assets
def download_asset(session: requests.Session, url: str, dest_dir: Path) -> str:
    """Качает файл, сохраняет в dest_dir. Возвращает имя файла."""
    resp = session.get(url, timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()

    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12] + guess_ext(url)
    dest = dest_dir / name
    if not dest.exists():
        dest.write_bytes(resp.content)
    return dest.name


def process_images(session, soup: BeautifulSoup, base_url: str, assets_dir: Path) -> int:
    """Скачивает картинки в assets_dir и переписывает src на локальные пути."""
    count = 0
    for img in soup.find_all("img"):
        src = None
        for attr in IMAGE_SRC_ATTRS:
            if img.get(attr):
                src = img[attr]
                break
        if not src:
            continue

        abs_url = urljoin(base_url, src)
        try:
            name = download_asset(session, abs_url, assets_dir)
        except Exception as exc:
            # не падаем из-за одной картинки — оставляем исходную ссылку
            log.debug("  [asset] не скачалась %s: %s", abs_url, exc)
            continue

        rel = f"{assets_dir.name}/{name}"
        img["src"] = rel
        # убираем srcset/lazy-атрибуты, чтобы ничего не ссылалось на сайт
        for attr in list(img.attrs):
            if "srcset" in attr or attr in ("data-src", "data-lazy-src", "data-orig-file"):
                del img[attr]
        count += 1
    return count


def inline_stylesheets(session, soup: BeautifulSoup, base_url: str) -> int:
    """Инлайнит <link rel=stylesheet> в <style> (для офлайн-стилизации)."""
    count = 0
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        if "stylesheet" not in rel:
            continue
        href = link["href"]
        if not href.startswith(("http://", "https://")):
            href = urljoin(base_url, href)
        try:
            resp = session.get(href, timeout=TIMEOUT, verify=VERIFY_SSL)
            resp.raise_for_status()
            css = resp.content.decode("utf-8", errors="replace")
        except Exception as exc:
            log.debug("  [css] не скачался %s: %s", href, exc)
            continue
        style = soup.new_tag("style")
        style.string = css
        link.replace_with(style)
        count += 1
    return count


# ------------------------------------------------------- скачивание отчёта
def download_report(session: requests.Session, url: str, out_dir: Path) -> Path:
    resp = session.get(url, timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()

    final_url = resp.url  # реальный адрес после редиректов
    soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")

    stem = make_stem(final_url)

    # каждый отчёт — в свою подпапку, картинки — внутри неё в images/
    report_dir = out_dir / stem
    report_dir.mkdir(exist_ok=True)
    html_path = report_dir / f"{stem}.html"
    assets_dir = report_dir / "images"

    if DOWNLOAD_IMAGES:
        assets_dir.mkdir(exist_ok=True)
        imgs = process_images(session, soup, final_url, assets_dir)
        if imgs:
            log.info("  %s: картинок привязано: %d", stem, imgs)

    if INLINE_STYLES:
        css = inline_stylesheets(session, soup, final_url)
        if css:
            log.info("  %s: стилей инлайн: %d", stem, css)

    # локальные картинки ломает <base> -> удаляем его, если он есть
    for b in soup.find_all("base"):
        b.decompose()

    if STRIP_SCRIPTS:
        for s in soup.find_all("script"):
            s.decompose()

    # убираем пустую папку images, если не привязали ни одной картинки
    if DOWNLOAD_IMAGES:
        try:
            assets_dir.rmdir()
        except OSError:
            pass

    html_path.write_text(str(soup), encoding="utf-8")
    return html_path


# ------------------------------------------------------- основной цикл
def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    conn = init_db(DB_PATH)
    session = make_session()

    log.info("Загружаю ленту: %s", FEED_URL)
    try:
        xml = fetch_feed(session, FEED_URL)
    except Exception as exc:
        log.error("Не удалось получить ленту %s: %s", FEED_URL, exc)
        conn.close()
        return 1

    feed = feedparser.parse(xml)
    if not feed.entries:
        log.error("Лента пуста или не распарсилась: %s",
                  getattr(feed, "bozo_exception", "нет данных"))
        conn.close()
        return 1

    new_found = 0
    downloaded = 0
    failed = 0
    seen_this_run: set[str] = set()

    for entry in feed.entries:
        guid = entry_guid(entry)
        url = normalize_url(entry.get("link", ""))

        if guid in seen_this_run or is_seen(conn, guid):
            continue
        seen_this_run.add(guid)
        new_found += 1

        title = entry.get("title", "")
        published = None
        if entry.get("published_parsed"):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()

        log.info("[НОВАЯ] %s | %s", title, url)
        try:
            path = download_report(session, url, OUT_DIR)
        except Exception as exc:
            # НЕ помечаем как обработанную: в следующий прогон попробуем снова
            log.error("Ошибка скачивания %s: %s", url, exc)
            failed += 1
            continue

        mark_seen(conn, guid, url, title, published, path)
        downloaded += 1

        time.sleep(REQUEST_DELAY)

    log.info("Готово. Новых в ленте: %d, скачано: %d, ошибок: %d. Отчёты в: %s",
             new_found, downloaded, failed, OUT_DIR)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
