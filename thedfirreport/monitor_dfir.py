#!/usr/bin/env python3
"""
Монитор аналитических отчётов JET CSIRT (https://jetcsirt.su/analytics/).

Сайт без RSS, контент отдаётся серверно (Битрикс).

Структура:
  - каталог : div.news-cards__item -> a[href] h3.news-title (+ p.news-subtitle);
    пагинация ?PAGEN_1=N (берём из .load-more[data-url] / навигации);
  - отчёт   : h1.page-main-heading, дата .tags-wrapper .date (DD.MM.YYYY),
    тело .post-content, внутри ссылка «Смотреть отчет» на .pdf.

Что делает:
  - обходит страницы каталога, останавливается на уже обработанных записях;
  - для каждого нового отчёта создаёт ОТДЕЛЬНУЮ ПАПКУ:
      index.html  - читабельная версия (заголовок, дата, тело, стили);
      assets/     - картинки из статьи (хэш-имена, пути переписаны локально);
      report-<hash>.pdf - скачанный PDF, ссылка в HTML ведёт на локальный файл;
  - дедупликация в SQLite; упавшие записи не помечаются обработанными.
"""

import hashlib
import html as html_module
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

# ------------------------------------------------------- настройки
SITE_URL = "https://jetcsirt.su/analytics/"
ROOT_DIR = Path("jetcsirt_analytics")      # внутри - папка на каждый отчёт
DB_PATH  = Path("seen_jetcsirt.db")

VERIFY_SSL = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 AnalyticsMonitor/1.0"
)

EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://jetcsirt.su/",
}

TIMEOUT = 30
RETRIES = 2
REQUEST_DELAY = 1.0
MAX_PAGES = 10

DOWNLOAD_IMAGES = True   # качать картинки статьи в assets/
DOWNLOAD_PDF = True      # качать PDF «Смотреть отчет»

# расширения, считающиеся «картинкой»
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".bmp"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("monitor")

if VERIFY_SSL is False:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

READER_CSS = """
    body { font-family: 'Segoe UI', Arial, sans-serif; color: #1f2328;
           line-height: 1.6; margin: 0; background: #fff; }
    .reader-wrap { max-width: 860px; margin: 0 auto; padding: 32px 20px 64px; }
    h1 { font-size: 30px; line-height: 1.25; margin: 8px 0 4px; }
    .meta { color: #6b7280; font-size: 14px; margin-bottom: 24px; }
    h2, h3 { margin: 24px 0 10px; }
    p { margin: 14px 0; }
    img { max-width: 100%; height: auto; border-radius: 8px; display: block; margin: 12px 0; }
    a { color: #163BFF; }
    ul, ol { padding-left: 22px; }
    .btn-white-outline.big { display: inline-block; margin: 8px 0 16px;
        padding: 10px 22px; border: 1px solid #163BFF; border-radius: 8px;
        color: #163BFF; text-decoration: none; font-weight: 500; }
"""


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
            folder_path TEXT,
            index_path  TEXT,
            pdf_names   TEXT,
            first_seen TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, guid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM reports WHERE guid = ?", (guid,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, guid, url, title, published,
              folder_path, index_path, pdf_names) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO reports "
        "(guid, url, title, published, folder_path, index_path, pdf_names) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guid, url, title, published, folder_path, index_path,
         json.dumps(pdf_names or [], ensure_ascii=False)),
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


def clean_text(text: str) -> str:
    text = html_module.unescape(text or "")
    text = text.replace("\xa0", " ").replace("\u2011", "-")
    return re.sub(r"\s+", " ", text).strip()


def make_stem(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.UNICODE)
    if not slug:
        slug = "reports"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def origin_of(url: str) -> str | None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def parse_date(text: str) -> str | None:
    """'01.04.2026' -> '2026-04-01'."""
    m = re.fullmatch(r"\s*(\d{2})\.(\d{2})\.(\d{4})\s*", text or "")
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def safe_url(url: str) -> str:
    """Квотит не-ASCII символы (кириллица в /upload/...) для запроса."""
    return requests.utils.requote_uri(url)


def download_file(session, url: str, dest: Path) -> bool:
    resp = session.get(safe_url(url), timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return True


def ext_of(url: str) -> str:
    path = urlparse(url).path.lower()
    m = re.search(r"(\.[a-z0-9]{2,5})$", path)
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


def fetch(session: requests.Session, url: str) -> requests.Response:
    resp = session.get(safe_url(url), timeout=TIMEOUT, verify=VERIFY_SSL)
    resp.raise_for_status()
    return resp


# ------------------------------------------------------- каталог
def parse_catalog(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_urls = set()
    for card in soup.select(".news-cards__item"):
        a = card.select_one("a[href] h3") or card.select_one("h3 a[href]")
        if a is None:
            # страховка: если h3 обёрнут в <a>
            a = card.find("a", href=True)
        if a is None:
            continue

        href = a.get("href") or ""
        if not href:
            continue
        url = normalize_url(urljoin(base_url, href))
        if url in seen_urls:
            continue
        seen_urls.add(url)

        h3 = card.select_one("h3.news-title")
        title = h3.get_text(" ", strip=True) if h3 else clean_text(a.get_text(" ", strip=True))
        sub = card.select_one("p.news-subtitle")
        subtitle = sub.get_text(" ", strip=True) if sub else ""

        items.append({"url": url, "title": title, "subtitle": subtitle})
    return items


def next_page_url(html: str, base_url: str) -> str | None:
    """URL следующей страницы пагинации (?PAGEN_1=N), если есть."""
    soup = BeautifulSoup(html, "html.parser")
    lm = soup.select_one(".load-more[data-url]")
    if lm is not None and lm.get("data-url"):
        return urljoin(base_url, lm["data-url"])
    # запасной вариант: стрелка вперёд / ссылка на вторую страницу
    for a in soup.select("a[href*='PAGEN_1=']"):
        href = urljoin(base_url, a["href"])
        qs = urlparse(href).query
        if re.search(r"PAGEN_1=(\d+)", qs):
            page = int(re.search(r"PAGEN_1=(\d+)", qs).group(1))
            if page > 1:
                return href
    return None


# ------------------------------------------------------- отчёт + ассеты
def strip_vue_attrs(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        for key in list(tag.attrs.keys()):
            if key.startswith("v-") or key.startswith(":") or key.startswith("@"):
                del tag.attrs[key]
    for tag in soup.find_all(attrs={"hidden": True}):
        del tag.attrs["hidden"]


def download_report(session, url: str, root_dir: Path):
    """
    Качает страницу отчёта, собирает папку со всеми ассетами.
    Возвращает (folder_path, index_path, pdf_names).
    """
    resp = fetch(session, url)
    final_url = resp.url
    soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")

    # заголовок
    title = None
    h1 = soup.select_one("h1.page-main-heading") or soup.select_one("h1")
    if h1 is not None:
        title = clean_text(h1.get_text(" ", strip=True))

    # дата
    date = None
    date_el = soup.select_one(".tags-wrapper .date")
    if date_el is not None:
        date = parse_date(date_el.get_text(" ", strip=True))

    # тело
    body_el = soup.select_one(".post-content")
    if body_el is None:
        raise RuntimeError("Не найден блок .post-content")

    # --------------------------------------------------------- папка отчёта
    stem = make_stem(final_url)
    folder = root_dir / stem
    assets_dir = folder / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(exist_ok=True)

    pdf_names = []

    # --- картинки в assets/ ---
    if DOWNLOAD_IMAGES:
        for img in body_el.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            abs_url = urljoin(final_url, src)
            ext = ext_of(abs_url)
            if ext not in IMAGE_EXTS:
                continue
            name = hashlib.sha256(abs_url.encode("utf-8")).hexdigest()[:12] + ext
            dest = assets_dir / name
            try:
                download_file(session, abs_url, dest)
            except Exception as exc:
                log.debug("  [img] не скачалась %s: %s", abs_url, exc)
                continue
            img["src"] = f"assets/{name}"
            # убираем srcset/lazy-атрибуты, чтобы не тянули оригинал
            for attr in list(img.attrs):
                if "srcset" in attr or attr in ("data-src", "data-lazy-src"):
                    del img[attr]

    # --- PDF-ссылки («Смотреть отчет») ---
    if DOWNLOAD_PDF:
        for a in body_el.find_all("a", href=True):
            href = a["href"]
            abs_url = urljoin(final_url, href)
            if ext_of(abs_url).lower() != ".pdf":
                continue
            name = f"report-{hashlib.sha256(abs_url.encode('utf-8')).hexdigest()[:8]}.pdf"
            dest = folder / name
            try:
                download_file(session, abs_url, dest)
            except Exception as exc:
                log.debug("  [pdf] не скачался %s: %s", abs_url, exc)
                continue
            a["href"] = name          # локальная ссылка в той же папке
            a["target"] = "_self"
            pdf_names.append(name)

    # убираем лишнее
    strip_vue_attrs(body_el)

    # --------------------------------------------------------- HTML
    meta = []
    if date:
        meta.append(f"Дата: {date}")
    meta.append(f"Источник: {final_url}")
    if pdf_names:
        meta.append(f"PDF: {'; '.join(pdf_names)}")

    page = (
        "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n"
        f"<meta charset=\"utf-8\">\n"
        f"<title>{html_module.escape(title or 'Отчет')}</title>\n"
        f"<style>{READER_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"reader-wrap\">\n"
        + "<h1>" + (html_module.escape(title) if title else "Без заголовка") + "</h1>\n"
        + ("<div class=\"meta\">" + " · ".join(meta) + "</div>\n" if meta else "")
        + str(body_el)
        + "\n</div>\n</body>\n</html>"
    )

    index_path = folder / "index.html"
    index_path.write_text(page, encoding="utf-8")

    # если папка assets пустая — удаляем
    try:
        assets_dir.rmdir()
    except OSError:
        pass

    return folder, index_path, pdf_names


# ------------------------------------------------------- основной цикл
def main() -> int:
    ROOT_DIR.mkdir(exist_ok=True)
    conn = init_db(DB_PATH)
    session = make_session()

    items = []                # все новые кандидаты со всех страниц каталога
    seen_this_run: set[str] = set()
    page_url = SITE_URL

    for _page in range(1, MAX_PAGES + 1):
        log.info("Каталог, страница %d: %s", _page, page_url)
        try:
            html = fetch(session, page_url).content.decode("utf-8", errors="replace")
        except Exception as exc:
            log.error("Ошибка загрузки каталога %s: %s", page_url, exc)
            break

        page_items = parse_catalog(html, page_url)
        if not page_items:
            break

        hit_processed = False
        for it in page_items:
            if it["url"] in seen_this_run:
                continue
            seen_this_run.add(it["url"])
            items.append(it)
            if is_seen(conn, it["url"]):
                hit_processed = True

        if hit_processed:
            log.info("Дошли до уже обработанных записей — останавливаемся.")
            break

        nxt = next_page_url(html, page_url)
        if not nxt or normalize_url(nxt) == normalize_url(page_url):
            break
        page_url = nxt
        time.sleep(REQUEST_DELAY)

    log.info("В каталоге новых кандидатов: %d", len(items))

    new_found = 0
    ok = 0
    failed = 0

    for item in items:
        guid = item["url"]
        if is_seen(conn, guid):
            continue
        new_found += 1

        log.info("[НОВАЯ] %s | %s", item["title"], item["url"])
        try:
            folder, index_path, pdf_names = download_report(session, item["url"], ROOT_DIR)
        except Exception as exc:
            log.error("Ошибка обработки %s: %s", item["url"], exc)
            failed += 1
            continue

        mark_seen(conn, guid, item["url"], item["title"], item.get("subtitle"),
                  str(folder), str(index_path), pdf_names)
        ok += 1
        if pdf_names:
            log.info("  -> %s (+ PDF: %s)", index_path.name, ", ".join(pdf_names))
        else:
            log.info("  -> %s", index_path.name)

        time.sleep(REQUEST_DELAY)

    log.info("Готово. Новых: %d, обработано: %d, ошибок: %d. Папки в: %s",
             new_found, ok, failed, ROOT_DIR)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
