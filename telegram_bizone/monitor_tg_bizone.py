#!/usr/bin/env python3
"""
Web-захват сообщений канала bin_z0ne через web.telegram.org (Playwright).
ПЛАН Б: когда MTProto (Telethon) не отдаёт контент, а в вебе он есть.

Картинки вытаскиваются из blob-URL прямо в JavaScript внутри страницы
(fetch(blobUrl) -> base64) и сохраняются по MIME-типу. HTML поста
берётся из DOM ([data-rich-copy-root]) с полным форматированием.
Скриншоты сообщений НЕ делаем.

Структура выгрузки:
  telegram_bizone/bin_z0ne/<дата>_<id>/  (или msg_<id> если дата не распарсилась)
      <имя папки>.html
      images/<hash>.jpg|png|webp|...

Env:
  TG_WEB_STORAGE   — JSON storage state (обязательно)
  TG_CHANNEL_HASH  — внутр. id канала (по умолч. -1001123893738)
  TG_POST_IDS      — "458,460,461" только эти id (тест)
  TG_SCAN_LIMIT    — последние N (по умолч. 20)
  TG_HEADLESS      — "0"/"false" — видимое окно браузера
"""
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ------------------------------------------------------- настройки
CHANNEL = "bin_z0ne"
CHANNEL_WEB_ID = os.environ.get("TG_CHANNEL_HASH", "-1001123893738")
OUT_DIR = Path("telegram_bizone/bin_z0ne")
DB_PATH = Path("telegram_bizone/seen_binzone.db")
SCAN_LIMIT = int(os.environ.get("TG_SCAN_LIMIT", 20))
POST_IDS = [int(x) for x in os.environ.get("TG_POST_IDS", "").split(",") if x.strip()]
HEADLESS = os.environ.get("TG_HEADLESS", "1") not in ("0", "false", "no")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tg-web-capture")

RUS_MONTHS_INV = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
    "июня": 6, "июля": 7, "августа": 8, "сентября": 9,
    "октября": 10, "ноября": 11, "декабря": 12,
}
MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


# ------------------------------------------------------- хранилище
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            msg_id      INTEGER PRIMARY KEY,
            date        TEXT,
            media_count INTEGER DEFAULT 0,
            dir_name    TEXT,
            empty       INTEGER DEFAULT 0,
            first_seen  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def seen_ids(conn):
    return {r[0] for r in conn.execute("SELECT msg_id FROM messages").fetchall()}


def mark(conn, msg_id: int, dir_name: str, media_count: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO messages (msg_id, date, media_count, dir_name, empty) "
        "VALUES (?, ?, ?, ?, 0)",
        (msg_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         media_count, dir_name),
    )
    conn.commit()


# ------------------------------------------------------- DOM-хелперы
def extract_ids(page, limit: int) -> list[int]:
    return page.evaluate(
        """(limit) => {
            const out = [];
            document.querySelectorAll('[data-message-id]').forEach(e => {
                const v = parseInt(e.getAttribute('data-message-id'), 10);
                if (Number.isFinite(v)) out.push(v);
            });
            return [...new Set(out)].sort((a, b) => b - a).slice(0, limit);
        }""",
        limit,
    )


def scroll_to_message(page, mid: int, max_scrolls: int = 150):
    for _ in range(max_scrolls):
        el = page.query_selector(f'[data-message-id="{mid}"]')
        if el:
            try:
                el.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            page.wait_for_timeout(1600)
            return el
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(250)
    return None


def get_date_label(page, el) -> str:
    return page.evaluate(
        """(el) => {
            const g = el.closest('.message-date-group');
            if (!g) return '';
            const sd = g.querySelector('.sticky-date, .sticky-date-group');
            if (!sd) return '';
            return sd.getAttribute('title') || sd.textContent || '';
        }""",
        el,
    ) or ""


def parse_russian_date(s: str):
    m = re.match(r"\s*(\d{1,2})\s+([а-яА-ЯёЁ]+)(?:\s+(\d{4}))?\s*$", s or "")
    if not m:
        return None
    day = int(m.group(1))
    month = RUS_MONTHS_INV.get(m.group(2).lower().rstrip(".,"))
    if not month:
        return None
    year = int(m.group(3)) if m.group(3) else datetime.now().year
    try:
        d = datetime(year, month, day).date()
    except ValueError:
        return None
    if not m.group(3) and d > datetime.now().date() + timedelta(days=30):
        d = datetime(year - 1, month, day).date()
    return d


def fetch_blob_images(page, el) -> dict:
    return page.evaluate(
        """async (el) => {
            const out = {};
            const imgs = el.querySelectorAll('img');
            for (const img of imgs) {
                const src = img.currentSrc || img.src || '';
                if (!src.startsWith('blob:')) continue;
                if (out[src]) continue;
                try {
                    const resp = await fetch(src);
                    if (!resp.ok) continue;
                    const blob = await resp.blob();
                    const buf = await blob.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let binary = '';
                    const CH = 0x8000;
                    for (let i = 0; i < bytes.length; i += CH) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
                    }
                    out[src] = { b64: btoa(binary), mime: blob.type };
                } catch (e) { /* пропускаем */ }
            }
            return out;
        }""",
        el,
    )


def get_rich_html(page, el) -> str:
    root = page.evaluate(
        """(el) => {
            const r = el.querySelector('[data-rich-copy-root]') || el.querySelector('.content-inner');
            if (r) return r.innerHTML;
            return '';
        }""",
        el,
    )
    if root and root.strip():
        return root
    text = el.inner_text() if hasattr(el, "inner_text") else ""
    return "".join(f"<p>{line}</p>" for line in text.split("\n") if line.strip())


def sanitize_html(frag: str) -> str:
    soup = BeautifulSoup(frag, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "link", "meta", "svg", "canvas"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]
        if tag.name == "a":
            tag["target"] = "_blank"
    return str(soup)


def save_blob_images(page, el, images_dir: Path, html_frag: str):
    blobs = fetch_blob_images(page, el)
    saved = []
    for src, data in blobs.items():
        try:
            raw = base64.b64decode(data["b64"])
        except Exception as exc:
            log.warning("    не декодировалось %s: %s", src, exc)
            continue
        mime = data.get("mime", "")
        ext = MIME_EXT.get(mime, ".jpg")
        name = hashlib.sha256(src.encode("utf-8")).hexdigest()[:12] + ext
        (images_dir / name).write_bytes(raw)
        saved.append(name)
        html_frag = html_frag.replace(src, f"images/{name}")
        log.info("    img -> %s (%s, %d байт)", name, mime or "?", len(raw))
    return html_frag, saved


def build_page(channel_name: str, mid: int, date_label: str, body_html: str) -> str:
    web_link = f"https://web.telegram.org/a/#{CHANNEL_WEB_ID}/{mid}"
    tme_link = f"https://t.me/{channel_name}/{mid}"
    title = (re.sub(r"<[^>]+>", " ", body_html) or f"Сообщение {mid}").strip()
    title = re.sub(r"\s+", " ", title)[:120]
    import html as htmlmod
    title = htmlmod.escape(title)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ max-width: 900px; margin: 24px auto; padding: 0 16px;
         font-family: -apple-system, Arial, sans-serif; line-height: 1.6; color: #111; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 8px; }}
  img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 6px 0; }}
  pre {{ background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }}
  code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 4px; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 8px 0; padding-left: 12px; color: #444; }}
  table {{ border-collapse: collapse; margin: 10px 0; }}
  td, th {{ border: 1px solid #ccc; padding: 6px 10px; }}
  p {{ margin: 8px 0; }}
</style>
</head>
<body>
<p class="meta">ID: {mid} · {date_label or ''} · <a href="{web_link}" target="_blank">открыть в вебе</a> · <a href="{tme_link}" target="_blank">t.me/{mid}</a></p>
<hr>
{body_html}
</body>
</html>
"""


# ------------------------------------------------------- основной цикл
def main() -> int:
    storage_raw = os.environ.get("TG_WEB_STORAGE", "").strip()
    if not storage_raw:
        log.error("Нет секрета TG_WEB_STORAGE.")
        return 1
    try:
        storage = json.loads(storage_raw)
    except Exception:
        log.error("TG_WEB_STORAGE не является JSON.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db()
    done = seen_ids(conn)

    new_count = 0
    err = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  storage_state=storage)
        page = ctx.new_page()

        try:
            page.goto(f"https://web.telegram.org/a/#{CHANNEL_WEB_ID}",
                      wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log.error("Не открылся канал: %s", exc)
            browser.close()
            return 1

        try:
            page.wait_for_selector("[data-message-id]", timeout=45000)
        except Exception as exc:
            log.error("Не дождались сообщений (storage не подошёл?): %s", exc)
            browser.close()
            return 1

        page.wait_for_timeout(3000)
        ids = POST_IDS or extract_ids(page, SCAN_LIMIT)
        log.info("Обрабатываю id: %s", ids)

        for mid in ids:
            if mid in done:
                continue

            el = scroll_to_message(page, mid)
            if not el:
                log.warning("  msg %s: не удалось найти в ленте", mid)
                err += 1
                continue

            try:
                body_html = get_rich_html(page, el)
                date_label = get_date_label(page, el)
                if not (body_html or "").strip():
                    log.warning("  msg %s: в вебе контент не отрисовался", mid)
                    continue
            except Exception as exc:
                log.error("  msg %s: не удалось прочитать DOM: %s", mid, exc)
                err += 1
                continue

            d = parse_russian_date(date_label)
            dir_name = f"{d.isoformat()}_{mid}" if d else f"msg_{mid}"
            post_dir = OUT_DIR / dir_name
            images_dir = post_dir / "images"
            post_dir.mkdir(parents=True, exist_ok=True)
            images_dir.mkdir(exist_ok=True)

            try:
                body_html, img_names = save_blob_images(page, el, images_dir, body_html)
            except Exception as exc:
                log.error("  msg %s: ошибка картинок: %s", mid, exc)
                err += 1
                continue

            if not img_names:
                log.warning("  msg %s: картинок из blob не достали (медиа, возможно, видео)", mid)

            if not img_names:
                try:
                    images_dir.rmdir()
                except OSError:
                    pass

            body_html = sanitize_html(body_html)

            try:
                page_html = build_page(CHANNEL, mid, date_label.strip(), body_html)
                (post_dir / f"{dir_name}.html").write_text(page_html, encoding="utf-8")
            except Exception as exc:
                log.error("  msg %s: не сохранился HTML: %s", mid, exc)
                err += 1
                continue

            mark(conn, mid, dir_name, len(img_names))
            new_count += 1
            log.info("[НОВОЕ] %s | картинок=%d", dir_name, len(img_names))

        browser.close()

    conn.close()
    log.info("Готово. Захвачено: %d, ошибок: %d. Файлы в: %s",
             new_count, err, OUT_DIR)
    return 1 if err else 0


if __name__ == "__main__":
    raise SystemExit(main())
