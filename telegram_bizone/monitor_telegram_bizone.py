#!/usr/bin/env python3
"""
Монитор Telegram-канала https://t.me/bin_z0ne — выгрузка сообщений
и медиа в репозиторий через GitHub Actions.

Логика — по аналогии с DFIR:
  - проверяем последние 20 сообщений канала;
  - каждое НОВОЕ сообщение сохраняем в отдельную папку:
        telegram_bizone/bin_z0ne/<дата>_<msg_id>/
            <дата>_<msg_id>.html
            images/<файл.png|jpg|...>
  - дедупликация по msg_id в SQLite (telegram_bizone/seen_binzone.db):
    уже обработанные сообщения пропускаются, качаются только новые.

Запуск:
    python telegram_bizone/monitor_telegram_bizone.py

Переменные окружения (секреты GitHub):
    TG_API_ID           — целое число api_id
    TG_API_HASH         — строка api_hash
    TG_SESSION_STRING   — session string (сгенерирована локально)
    TG_SCAN_LIMIT       — (опционально) сколько последних сообщений сканировать,
                          по умолчанию 20.
"""

import asyncio
import html
import logging
import os
import sqlite3
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

# ------------------------------------------------------- настройки
CHANNEL = "bin_z0ne"                          # https://t.me/bin_z0ne
OUT_DIR = Path("telegram_bizone/bin_z0ne")    # посты сохраняем рядом со скриптом
DB_PATH = Path("telegram_bizone/seen_binzone.db")

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION = os.environ["TG_SESSION_STRING"]

# сколько последних сообщений проверяем — по умолчанию 20
SCAN_LIMIT = int(os.environ.get("TG_SCAN_LIMIT", 20))
if SCAN_LIMIT <= 0:
    SCAN_LIMIT = None   # 0 = неограниченно (вся история)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tg-bizone-monitor")


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
            first_seen  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def seen_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT msg_id FROM messages").fetchall()
    return {r[0] for r in rows}


def mark_done(conn: sqlite3.Connection, msg_id: int, date_iso: str,
              media_count: int, dir_name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO messages (msg_id, date, media_count, dir_name) "
        "VALUES (?, ?, ?, ?)",
        (msg_id, date_iso, media_count, dir_name),
    )
    conn.commit()


# ------------------------------------------------------- рендер HTML
def text_to_html(text: str | None) -> str:
    if not text:
        return ""
    blocks = []
    for line in html.escape(text).split("\n"):
        if line.strip():
            blocks.append(f"<p>{line}</p>")
    return "\n".join(blocks)


def render_html(msg, media_names: list[str]) -> str:
    title = (msg.text or f"Сообщение {msg.id}").split("\n")[0]
    title = html.escape(title)[:120]

    date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S UTC") if msg.date else ""
    original = f"https://t.me/{CHANNEL}/{msg.id}"

    body = [text_to_html(msg.text)]

    for name in media_names:
        low = name.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")):
            body.append(f'<p><img src="images/{html.escape(name)}" '
                        f'style="max-width:100%;height:auto;" loading="lazy"></p>')
        else:
            body.append(f'<p><a href="images/{html.escape(name)}">📎 {html.escape(name)}</a></p>')

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ max-width: 860px; margin: 24px auto; padding: 0 16px;
         font-family: Arial, sans-serif; line-height: 1.55; }}
  .meta {{ color: #666; font-size: 13px; }}
  p {{ overflow-wrap: anywhere; }}
  img {{ border: 1px solid #ddd; border-radius: 6px; margin: 8px 0; }}
</style>
</head>
<body>
<h1>@{CHANNEL}</h1>
<p class="meta">Дата: {date_str} · ID: {msg.id} ·
<a href="{original}" target="_blank">открыть оригинал</a></p>
<hr>
{chr(10).join(body)}
</body>
</html>
"""


# ------------------------------------------------------- медиа
async def download_media(client, msg, images_dir: Path) -> list[str]:
    """Скачивает медиа сообщения в images_dir. Возвращает имена файлов."""
    if msg.media is None:
        return []

    result = await client.download_media(msg, file=str(images_dir))
    if result is None:
        return []

    if isinstance(result, (list, tuple)):
        names = [Path(x).name for x in result if x]
    else:
        names = [Path(result).name]
    return names


# ------------------------------------------------------- основной цикл
async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db()
    done = seen_ids(conn)

    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        log.error("Сессия не авторизована — проверь TG_SESSION_STRING.")
        await client.disconnect()
        conn.close()
        return 1

    me = await client.get_me()
    log.info("Залогинен как @%s", getattr(me, "username", str(me.id)))

    channel = await client.get_entity(CHANNEL)
    log.info("Канал: %s | сканирую последние %s сообщений",
             getattr(channel, "title", CHANNEL),
             SCAN_LIMIT if SCAN_LIMIT else "все")

    new_count = 0
    err_count = 0

    async for msg in client.iter_messages(channel, limit=SCAN_LIMIT):
        if msg.id in done:
            continue                 # уже обработано ранее
        done.add(msg.id)

        date_str = msg.date.strftime("%Y-%m-%d") if msg.date else "nodate"
        dir_name = f"{date_str}_{msg.id}"
        post_dir = OUT_DIR / dir_name
        images_dir = post_dir / "images"
        post_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(exist_ok=True)

        media_names = []
        try:
            media_names = await download_media(client, msg, images_dir)
        except Exception as exc:
            log.warning("  msg %s: медиа не скачалось: %s", msg.id, exc)
            err_count += 1

        # пустую папку images убираем, если медиа не было
        if not media_names:
            try:
                images_dir.rmdir()
            except OSError:
                pass

        try:
            html_path = post_dir / f"{dir_name}.html"
            html_path.write_text(render_html(msg, media_names), encoding="utf-8")
        except Exception as exc:
            log.error("  msg %s: не сохранился HTML: %s", msg.id, exc)
            err_count += 1
            continue

        mark_done(conn, msg.id, msg.date.isoformat()
                  if msg.date else "", len(media_names), dir_name)
        new_count += 1
        log.info("[НОВОЕ] %s | %d медиа | %s",
                 dir_name, len(media_names),
                 (msg.text or "")[:60].replace("\n", " "))

    log.info("Готово. Новых: %d, ошибок: %d. Файлы в: %s",
             new_count, err_count, OUT_DIR)

    await client.disconnect()
    conn.close()
    return 1 if err_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
