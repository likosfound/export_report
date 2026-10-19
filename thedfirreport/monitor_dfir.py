#!/usr/bin/env python3
"""
Монитор новых статей The DFIR Report (https://thedfirreport.com/).

У сайта есть RSS: https://thedfirreport.com/feed/ - используем его.

Особенности этой версии:
  1. Лента качается НАШИМ requests.Session (UA, VERIFY_SSL, retry),
     а не внутренним запросом feedparser - иначе в корпоративном
     прокси/Zscaler лента отдаётся пустой.
  2. Статьи (WordPress, серверный HTML) сохраняются с тегом <base> и без
     <script> - открываются с диска с картинками и разметкой.
  3. VERIFY_SSL = False из-за корпоративного TLS-перехвата.
  4. Нескачанные статьи НЕ помечаются обработанными -> перезапуск докачает.
"""

import hashlib
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import urllib3
from bs4 import BeautifulSoup

# ------------------------------------------------------- настройки
FEED_URL = "https://thedfirreport.com/feed/"
OUT_DIR  = Path("thedfirreport/dfir_reports")
DB_PATH  = Path("seen_dfir.db")

# False - НЕ проверять TLS: корпоративный прокси/Zscaler подменяет сертификаты.
VERIFY_SSL = False

# True = вырезать <script> и добавить <base>, чтобы файл открывался с диска.
STRIP_SCRIPTS = True

# ВАЖНО: только ASCII! Русский текст в HTTP-заголовках запрещён стандартом.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 DfirMonitor/1.0"
)

EXTRA_HEADERS = {
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://thedfirreport.com/",
}

TIMEOUT = 30
RETRIES = 2
REQUEST_DELAY = 1.0

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


def make_filename(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.UNICODE)
    if not slug:
        slug = "report"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.html"


def origin_of(url: str) -> str | None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


# ------------------------------------------------------- подготовка HTML
def strip_scripts(html: str) -> str:
    html = re.sub(r"<script\b[^>]*/>", "", html, flags=re.IGNORECASE)
    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return html


def prepare_html(html_bytes: bytes, url: str, strip: bool) -> bytes:
    origin = origin_of(url)
    text = html_bytes.decode("utf-8", errors="replace")

    if origin is not None and "<base" not in text.lower():
        base_tag = f'<base href="{origin}/">'
        head = re.search(r"<head[^>]*>", text, re.I)
        if head:
            pos = head.end()
            text = text[:pos] + base_tag + text[pos:]
        else:
            text = base_tag + text

    if strip:
        text = strip_scripts(text)

    return text.encode("utf-8")


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


def download_report(session: requests.Session, url: str, out_dir: Path) -> Path:
    resp = session.get(url, timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()

    final_url = resp.url  # реальный адрес после редиректов
    html = prepare_html(resp.content, final_url, STRIP_SCRIPTS)

    out_path = out_dir / make_filename(final_url)
    out_path.write_bytes(html)
    return out_path


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

    log.info("Готово. Новых в ленте: %d, скачано: %d, ошибок: %d. Файлы в: %s",
             new_found, downloaded, failed, OUT_DIR)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
