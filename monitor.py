#!/usr/bin/env python3
"""
Мониторинг Telegram-каналов с матчингом по позициям.

Юзербот (Telethon) слушает каналы/группы, на которые подписан аккаунт.
При совпадении сообщения с позицией — бот (BotFather) шлёт уведомление.

Позиции — набор ключевых слов через запятую. Все слова должны быть
в сообщении для совпадения. Примеры:
  /add iPhone 16 Pro, 256, Black Titanium
  /add MacBook Air, 15, M3
  /add iPad Pro, 13

Переменные окружения (.env):
  API_ID     — из https://my.telegram.org
  API_HASH   — из https://my.telegram.org
  BOT_TOKEN  — токен от @BotFather
  ADMIN_ID   — твой Telegram ID (число)
  PHONE      — номер телефона для юзербота (+7...)
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, Button, events
from telethon.tl.types import User

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PHONE = os.getenv("PHONE", "")

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "data.json"
SESSION_DIR = Path(__file__).parent / "sessions"

_copy_cache: dict[int, dict] = {}


# ── хранилище ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text("utf-8"))
    return {"positions": [], "channels": []}


def save_data(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


# ── матчинг ────────────────────────────────────────────────────────────────

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def match_positions(text: str, positions: list) -> list:
    n = norm(text)
    matched = []
    for pos in positions:
        keywords = pos if isinstance(pos, list) else [pos]
        if all(norm(kw) in n for kw in keywords):
            matched.append(keywords)
    return matched


# ── основной цикл ─────────────────────────────────────────────────────────

async def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    user = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)
    bot = TelegramClient(str(SESSION_DIR / "bot"), API_ID, API_HASH)

    await user.start(phone=PHONE)
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Юзербот и бот запущены. ADMIN_ID=%d", ADMIN_ID)

    # ── команды бота ───────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/start", from_users=ADMIN_ID))
    async def cmd_start(event):
        await event.reply(
            "🔍 Мониторинг каналов\n\n"
            "📦 Позиции:\n"
            "/add ключевые, слова, через, запятую\n"
            "/remove <номер>\n"
            "/list — все позиции\n"
            "/clear — очистить\n\n"
            "📡 Каналы:\n"
            "/watch @канал или ссылка\n"
            "/unwatch @канал\n"
            "/channels — список\n\n"
            "📎 Загрузка: отправь .csv, .json или .txt файл"
        )

    @bot.on(events.NewMessage(pattern="/add", from_users=ADMIN_ID))
    async def cmd_add(event):
        text = event.text.partition(" ")[2].strip()
        if not text:
            await event.reply(
                "Пример:\n"
                "/add iPhone 16 Pro, 256, Black Titanium\n"
                "/add MacBook Air, 15, M3"
            )
            return
        keywords = [k.strip() for k in text.split(",") if k.strip()]
        data = load_data()
        norm_new = [norm(k) for k in keywords]
        for pos in data["positions"]:
            if [norm(k) for k in pos] == norm_new:
                await event.reply(f"Уже есть: {', '.join(keywords)}")
                return
        data["positions"].append(keywords)
        save_data(data)
        await event.reply(
            f"✅ Добавлено: {' + '.join(keywords)}\n"
            f"Всего позиций: {len(data['positions'])}"
        )

    @bot.on(events.NewMessage(pattern="/remove", from_users=ADMIN_ID))
    async def cmd_remove(event):
        text = event.text.partition(" ")[2].strip()
        if not text:
            await event.reply("Использование: /remove 3 (номер из /list)")
            return
        try:
            idx = int(text) - 1
        except ValueError:
            await event.reply("Укажи номер позиции из /list")
            return
        data = load_data()
        if 0 <= idx < len(data["positions"]):
            removed = data["positions"].pop(idx)
            save_data(data)
            await event.reply(
                f"❌ Удалено: {' + '.join(removed)}\n"
                f"Осталось: {len(data['positions'])}"
            )
        else:
            await event.reply(f"Нет позиции с номером {idx + 1}")

    @bot.on(events.NewMessage(pattern="/list$", from_users=ADMIN_ID))
    async def cmd_list(event):
        data = load_data()
        if not data["positions"]:
            await event.reply("Список пуст. Добавь через /add")
            return
        lines = []
        for i, pos in enumerate(data["positions"]):
            lines.append(f"{i + 1}. {' + '.join(pos)}")
        await event.reply("📋 Позиции:\n" + "\n".join(lines))

    @bot.on(events.NewMessage(pattern="/clear$", from_users=ADMIN_ID))
    async def cmd_clear(event):
        data = load_data()
        data["positions"] = []
        save_data(data)
        await event.reply("Все позиции удалены.")

    @bot.on(events.NewMessage(pattern="/watch", from_users=ADMIN_ID))
    async def cmd_watch(event):
        text = event.text.partition(" ")[2].strip()
        if not text:
            await event.reply("Использование: /watch @channel_name")
            return
        try:
            entity = await user.get_entity(text)
            ch_id = entity.id
            ch_title = getattr(entity, "title", text)
        except Exception as e:
            await event.reply(f"Не удалось найти канал: {e}")
            return
        data = load_data()
        if ch_id in data["channels"]:
            await event.reply(f"Уже отслеживается: {ch_title}")
            return
        data["channels"].append(ch_id)
        save_data(data)
        await event.reply(
            f"✅ Отслеживаю: {ch_title}\n"
            f"Всего каналов: {len(data['channels'])}"
        )

    @bot.on(events.NewMessage(pattern="/unwatch", from_users=ADMIN_ID))
    async def cmd_unwatch(event):
        text = event.text.partition(" ")[2].strip()
        if not text:
            await event.reply("Использование: /unwatch @channel_name")
            return
        try:
            entity = await user.get_entity(text)
            ch_id = entity.id
            ch_title = getattr(entity, "title", text)
        except Exception:
            await event.reply(f"Не удалось найти канал: {text}")
            return
        data = load_data()
        if ch_id not in data["channels"]:
            await event.reply(f"Не в списке: {ch_title}")
            return
        data["channels"].remove(ch_id)
        save_data(data)
        await event.reply(f"❌ Убрано: {ch_title}")

    @bot.on(events.NewMessage(pattern="/channels$", from_users=ADMIN_ID))
    async def cmd_channels(event):
        data = load_data()
        if not data["channels"]:
            await event.reply("Нет отслеживаемых каналов.\nДобавь: /watch @channel")
            return
        lines = []
        for ch_id in data["channels"]:
            try:
                entity = await user.get_entity(ch_id)
                title = getattr(entity, "title", str(ch_id))
                uname = getattr(entity, "username", None)
                tag = f" (@{uname})" if uname else ""
                lines.append(f"• {title}{tag}")
            except Exception:
                lines.append(f"• ID: {ch_id}")
        await event.reply("📡 Каналы:\n" + "\n".join(lines))

    # ── загрузка файла с позициями ─────────────────────────────────────────

    @bot.on(events.NewMessage(
        from_users=ADMIN_ID,
        func=lambda e: e.document is not None,
    ))
    async def on_file(event):
        name = ""
        for attr in event.document.attributes:
            if hasattr(attr, "file_name"):
                name = attr.file_name
                break
        if not name.endswith((".csv", ".json", ".txt")):
            return

        raw = await event.download_media(bytes)
        text = raw.decode("utf-8-sig")
        data = load_data()
        added = 0

        if name.endswith(".json"):
            for item in json.loads(text):
                if isinstance(item, str):
                    kw = [k.strip() for k in item.split(",") if k.strip()]
                elif isinstance(item, list):
                    kw = [str(k).strip() for k in item if str(k).strip()]
                elif isinstance(item, dict):
                    kw = [str(v).strip() for v in item.values() if str(v).strip()]
                else:
                    continue
                if kw:
                    data["positions"].append(kw)
                    added += 1
        elif name.endswith(".csv"):
            for row in csv.reader(io.StringIO(text)):
                kw = [c.strip() for c in row if c.strip()]
                if kw:
                    data["positions"].append(kw)
                    added += 1
        elif name.endswith(".txt"):
            for line in text.splitlines():
                line = line.strip()
                if line:
                    kw = [k.strip() for k in line.split(",") if k.strip()]
                    data["positions"].append(kw)
                    added += 1

        save_data(data)
        await event.reply(
            f"✅ Загружено позиций: {added}\n"
            f"Всего: {len(data['positions'])}"
        )

    # ── мониторинг каналов (юзербот) ───────────────────────────────────────

    @user.on(events.NewMessage)
    async def on_channel_message(event):
        if not event.is_channel and not event.is_group:
            return

        msg_text = event.text or ""
        if not msg_text:
            return

        data = load_data()
        if not data["channels"] or not data["positions"]:
            return

        chat = await event.get_chat()
        if chat.id not in data["channels"]:
            return

        matched = match_positions(msg_text, data["positions"])
        if not matched:
            return

        # название канала
        ch_title = getattr(chat, "title", str(chat.id))

        # ссылка на сообщение
        ch_username = getattr(chat, "username", None)
        if ch_username:
            msg_link = f"https://t.me/{ch_username}/{event.id}"
        else:
            msg_link = f"https://t.me/c/{chat.id}/{event.id}"

        # отправитель
        sender = await event.get_sender()
        if isinstance(sender, User):
            name = f"{sender.first_name or ''} {sender.last_name or ''}".strip() or "—"
            if sender.username:
                buyer_line = f"{name} (@{sender.username})\nhttps://t.me/{sender.username}"
            else:
                buyer_line = f"{name} (tg://user?id={sender.id})"
        elif sender and hasattr(sender, "title"):
            buyer_line = sender.title
        else:
            buyer_line = "Аноним (пост канала)"

        # совпавшие позиции
        pos_str = "\n".join(f"  • {', '.join(m)}" for m in matched)

        # текст сообщения (обрезаем если длинный)
        display = msg_text[:800] + ("..." if len(msg_text) > 800 else "")

        notification = (
            f"📢 Совпадение!\n\n"
            f"Канал: {ch_title}\n\n"
            f"Ссылка: {msg_link}\n\n"
            f"Покупатель: {buyer_line}\n\n"
            f"Сообщение:\n{display}\n\n"
            f"Позиция:\n{pos_str}"
        )

        buttons = [
            [
                Button.inline("📋 Сообщение", data=b"copy_msg"),
                Button.inline("📋 Позиция", data=b"copy_pos"),
            ]
        ]

        try:
            sent = await bot.send_message(
                ADMIN_ID, notification, buttons=buttons, link_preview=False,
            )
            _copy_cache[sent.id] = {"msg": msg_text, "pos": pos_str}
            if len(_copy_cache) > 500:
                oldest = sorted(_copy_cache)[:250]
                for k in oldest:
                    del _copy_cache[k]
        except Exception:
            log.exception("Ошибка отправки уведомления")

    @bot.on(events.CallbackQuery(data=b"copy_msg"))
    async def cb_copy_msg(event):
        cached = _copy_cache.get(event.message_id)
        if cached:
            await event.respond(cached["msg"])
        else:
            await event.answer("Сообщение не найдено (устарело)")

    @bot.on(events.CallbackQuery(data=b"copy_pos"))
    async def cb_copy_pos(event):
        cached = _copy_cache.get(event.message_id)
        if cached:
            await event.respond(cached["pos"])
        else:
            await event.answer("Позиция не найдена (устарело)")

    log.info("Мониторинг запущен. Каналов: %d, позиций: %d",
             len(load_data()["channels"]), len(load_data()["positions"]))

    await asyncio.gather(
        user.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        sys.exit("Нужны API_ID и API_HASH — получи на https://my.telegram.org")
    if not BOT_TOKEN:
        sys.exit("Нужен BOT_TOKEN — создай бота через @BotFather")
    if not ADMIN_ID:
        sys.exit("Нужен ADMIN_ID — узнай через @userinfobot")
    asyncio.run(main())
