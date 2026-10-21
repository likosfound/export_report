#!/usr/bin/env python3
"""
Монитор Telegram-канала https://t.me/bin_z0ne — выгрузка сообщений
и медиа в репозиторий через GitHub Actions.

Если пост сохраняется ПУСТЫМ (нет текста и картинки), причины:
  1) у канала включена «Защита контента» — медиа скачать нельзя, а текста у
     поста нет (фото без подписи) => остаётся пустая оболочка с id и датой;
  2) это служебное/альбомное сообщение (закреп, смена аватара и т.п.).
Скрипт логирует для каждого нового поста тип медиа и длину текста,
чтобы такие случаи было видно сразу.

Запуск:
    python telegram_bizone/monitor_telegram_bizone.py

Переменные окружения (секреты GitHub):
    TG_API_ID, TG_API_HASH, TG_SESSION_STRING, TG_SCAN_LIMIT (по умолч. 20)
"""

import asyncio
import html
import logging
import os
import sqlite3
import sys
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.errors import SecurityError
from telethon.sessions import StringSession

# ------------------------------------------------------- настройки
CHANNEL = "bin_z0ne"
OUT_DIR = Path("telegram_bizone/bin_z0ne")
DB_PATH = Path("telegram_bizone/seen_binzone.db")

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION = os.environ["TG_SESSION_STRING"]

SCAN_LIMIT = int(os.environ.get("TG_SCAN_LIMIT", 20))
if SCAN_LIMIT <= 0:
    SCAN_LIMIT = None

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


# ------------------------------------------------------- диагностика сообщения
def describe_media(media) -> str:
    """Краткое описание медиа для логов."""
    if media is None:
        return "нет"
    cls = media.__class__.__name__
    extras = []
    photo = getattr(media, "photo", None)
    if photo is not None:
        extras.append(f"photo_id={getattr(photo, 'id', '?')}")
        sizes = getattr(photo, "sizes", []) or []
        extras.append(f"sizes={len(sizes)}")
    doc = getattr(media, "document", None)
    if doc is not None:
        mime = getattr(doc, "mime_type", "?")
        size = getattr(doc, "size", "?")
        extras.append(f"mime={mime},size={size}")
    if extras:
        return f"{cls}({', '.join(extras)})"
    return cls


def log_message_diag(msg) -> None:
    log.info("  msg %s: text=%r | media=%s | grouped=%s | action=%s | entities=%s",
             msg.id,
             (msg.text or "")[:80],
             describe_media(msg.media),
             getattr(msg, "grouped_id", None),
             msg.action.__class__.__name__ if getattr(msg, "action", None) else None,
             bool(getattr(msg, "entities", None)))


# ------------------------------------------------------- рендер HTML
def text_to_html(text: str | None) -> str:
    if not text:
        return ""
    blocks = []
    for line in html.escape(text).split("\n"):
        if line.strip():
            blocks.append(f"<p>{line}</p>")
    return "\n".join(blocks)


def render_html(msg, media_names: list[str], channel_id: int) -> str:
    title = (msg.text or f"Сообщение {msg.id}").split("\n")[0]
    title = html.escape(title)[:120]

    date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S UTC") if msg.date else ""
    # ссылка как в веб-версии: по внутреннему id канала (по просьбе пользователя)
    web_link = f"https://web.telegram.org/a/#{channel_id}"
    if msg.id:
        web_link = f"{web_link}/{msg.id}"
    tme_link = f"https://t.me/{CHANNEL}/{msg.id}"

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
<a href="{web_link}" target="_blank">открыть в вебе</a> ·
<a href="{tme_link}" target="_blank">t.me/{msg.id}</a></p>
<hr>
{chr(10).join(body)}
</body>
</html>
"""


# ------------------------------------------------------- подписка на канал
async def ensure_membership(client, channel) -> None:
    """Лучшая попытка вступить в канал, если аккаунт ещё не подписан."""
    try:
        await client(functions.channels.JoinChannelRequest(channel=channel))
        log.info("Вступили в канал @%s (или уже были подписаны).", CHANNEL)
    except Exception as exc:
        log.warning("Не удалось вступить в канал @%s: %s", CHANNEL, exc)


# ------------------------------------------------------- медиа
async def download_media(client, msg, images_dir: Path) -> list[str]:
    """Скачивает медиа сообщения в images_dir. Возвращает имена файлов."""
    if msg.media is None:
        return []

    try:
        result = await client.download_media(msg, file=str(images_dir))
    except SecurityError as exc:
        # защита канала от сохранения контента
        log.warning("  msg %s: канал запрещает сохранять медиа (SecurityError): %s",
                    msg.id, exc)
        return []
    except Exception as exc:
        log.warning("  msg %s: медиа не скачалось: %s", msg.id, exc)
        return []

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
    channel_id = getattr(channel, "id", "")
    log.info("Канал: %s | id: %s | сканирую последние %s сообщений",
             getattr(channel, "title", CHANNEL), channel_id,
             SCAN_LIMIT if SCAN_LIMIT else "все")

    await ensure_membership(client, channel)

    new_count = 0
    err_count = 0
    empty_count = 0

    async for msg in client.iter_messages(channel, limit=SCAN_LIMIT):
        if msg.id in done:
            continue
        done.add(msg.id)

        # диагностика: что именно приходит по каждому новому посту
        log_message_diag(msg)

        date_str = msg.date.strftime("%Y-%m-%d") if msg.date else "nodate"
        dir_name = f"{date_str}_{msg.id}"
        post_dir = OUT_DIR / dir_name
        images_dir = post_dir / "images"
        post_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(exist_ok=True)

        media_names = await download_media(client, msg, images_dir)

        if not media_names and not (msg.text or "").strip():
            empty_count += 1
            log.warning("  msg %s: ПОЛУЧИЛОСЬ ПУСТЫМ (нет текста и нет скачанного медиа)",
                        msg.id)

        # пустую папку images убираем, если медиа не было
        if not media_names:
            try:
                images_dir.rmdir()
            except OSError:
                pass

        try:
            html_path = post_dir / f"{dir_name}.html"
            html_path.write_text(render_html(msg, media_names, channel_id),
                                 encoding="utf-8")
        except Exception as exc:
            log.error("  msg %s: не сохранился HTML: %s", msg.id, exc)
            err_count += 1
            continue

        mark_done(conn, msg.id, msg.date.isoformat()
                  if msg.date else "", len(media_names), dir_name)
        new_count += 1

    log.info("Готово. Новых: %d, пустых: %d, ошибок: %d. Файлы в: %s",
             new_count, empty_count, err_count, OUT_DIR)

    await client.disconnect()
    conn.close()
    return 1 if err_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
