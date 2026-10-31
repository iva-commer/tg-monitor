#!/usr/bin/env python3
"""
Мониторинг Telegram-каналов с матчингом по позициям.

Управление через форум-группу с топиками:
  📥 Запросы — уведомления о совпадениях
  📡 Каналы — каналы для мониторинга (отправляй @канал или ссылку)
  📦 Позиции — позиции для поиска (ключевые слова через запятую)

Настройка: добавь бота в группу админом и отправь /setup
"""

import asyncio
import csv
import hashlib
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
from telethon.tl.functions.channels import GetForumTopicsRequest

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

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "data.json"
CONFIG_FILE = DATA_DIR / "config.json"
SESSION_DIR = Path(__file__).parent / "sessions"

_copy_cache: dict[int, dict] = {}

cfg = {
    "group_id": 0,
    "topic_requests": 0,
    "topic_channels": 0,
    "topic_positions": 0,
}


# ── хранилище ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text("utf-8"))
    return {"positions": [], "channels": []}


def save_data(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    return {}


def save_config(c: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2), "utf-8")


# ── утилиты ────────────────────────────────────────────────────────────────

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


def get_topic_id(message) -> int | None:
    if not message.reply_to:
        return None
    return message.reply_to.reply_to_top_id or message.reply_to.reply_to_msg_id


def pos_hash(keywords: list) -> str:
    key = "|".join(norm(k) for k in keywords)
    return hashlib.md5(key.encode()).hexdigest()[:8]


async def detect_topics(client, group_id: int) -> dict:
    try:
        entity = await client.get_entity(group_id)
        result = await client(GetForumTopicsRequest(
            channel=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        ))
        topics = {}
        for t in result.topics:
            name = t.title.lower().strip()
            if "запрос" in name:
                topics["requests"] = t.id
            elif "канал" in name:
                topics["channels"] = t.id
            elif "позиц" in name:
                topics["positions"] = t.id
        return topics
    except Exception as e:
        log.error("Ошибка определения топиков: %s", e)
        return {}


# ── основной цикл ─────────────────────────────────────────────────────────

async def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    user = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)
    bot = TelegramClient(str(SESSION_DIR / "bot"), API_ID, API_HASH)

    await user.start(phone=os.getenv("PHONE", ""))
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Юзербот и бот запущены. ADMIN_ID=%d", ADMIN_ID)

    # ── загрузка конфига ──────────────────────────────────────────────────

    saved = load_config()
    group_id = int(os.getenv("GROUP_ID", "0")) or saved.get("group_id", 0)

    if group_id:
        cfg["group_id"] = group_id
        topics = await detect_topics(bot, group_id)
        if not topics:
            topics = await detect_topics(user, group_id)
        cfg["topic_requests"] = topics.get("requests", 0)
        cfg["topic_channels"] = topics.get("channels", 0)
        cfg["topic_positions"] = topics.get("positions", 0)
        if all([cfg["topic_requests"], cfg["topic_channels"], cfg["topic_positions"]]):
            log.info("Группа: %d | Запросы: %d | Каналы: %d | Позиции: %d",
                     cfg["group_id"], cfg["topic_requests"],
                     cfg["topic_channels"], cfg["topic_positions"])
        else:
            log.warning("Не все топики найдены. Отправь /setup в группе.")

    # ── /setup ─────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/setup", from_users=ADMIN_ID))
    async def cmd_setup(event):
        if not event.is_group:
            await event.reply(
                "Отправь /setup в группе с топиками.\n\n"
                "Инструкция:\n"
                "1. Создай группу, включи «Темы»\n"
                "2. Создай топики: Запросы, Каналы, Позиции\n"
                "3. Добавь бота админом\n"
                "4. Отправь /setup в группе"
            )
            return

        gid = event.chat_id
        topics = await detect_topics(bot, gid)
        if not topics:
            topics = await detect_topics(user, gid)

        if not all(k in topics for k in ("requests", "channels", "positions")):
            found = ", ".join(f"{k}={v}" for k, v in topics.items())
            await event.reply(
                f"❌ Не все топики найдены!\n"
                f"Найдено: {found or 'ничего'}\n\n"
                f"Создай топики с названиями:\n"
                f"• Запросы\n• Каналы\n• Позиции\n\n"
                f"И отправь /setup ещё раз"
            )
            return

        cfg["group_id"] = gid
        cfg["topic_requests"] = topics["requests"]
        cfg["topic_channels"] = topics["channels"]
        cfg["topic_positions"] = topics["positions"]
        save_config({"group_id": gid})

        await event.reply(
            f"✅ Настроено!\n\n"
            f"📥 Запросы → топик {topics['requests']}\n"
            f"📡 Каналы → топик {topics['channels']}\n"
            f"📦 Позиции → топик {topics['positions']}\n\n"
            f"Отправляй каналы и позиции в соответствующие топики."
        )
        log.info("Setup: group=%d requests=%d channels=%d positions=%d",
                 gid, topics["requests"], topics["channels"], topics["positions"])

    # ── /start в личке ────────────────────────────────────────────────────

    @bot.on(events.NewMessage(
        pattern="/start", from_users=ADMIN_ID,
        func=lambda e: e.is_private,
    ))
    async def cmd_start(event):
        status = "✅ Группа настроена" if cfg["group_id"] else "⚠️ Группа не настроена"
        data = load_data()
        await event.reply(
            f"🔍 Мониторинг каналов\n\n"
            f"{status}\n"
            f"Каналов: {len(data['channels'])}\n"
            f"Позиций: {len(data['positions'])}\n\n"
            f"Управление — через топики в группе.\n"
            f"Добавь бота в группу и отправь /setup"
        )

    # ── сообщения в группе ────────────────────────────────────────────────

    @bot.on(events.NewMessage(func=lambda e: e.is_group and e.sender_id == ADMIN_ID))
    async def on_group_message(event):
        if not cfg["group_id"] or event.chat_id != cfg["group_id"]:
            return
        if event.document:
            return

        tid = get_topic_id(event.message)
        if not tid:
            return

        text = (event.text or "").strip()
        if not text or text.startswith("/setup"):
            return

        # ── топик «Каналы» ─────────────────────────────────────────────
        if tid == cfg["topic_channels"]:
            if text.lower() == "/list":
                data = load_data()
                if not data["channels"]:
                    await event.reply("Пусто. Отправь @канал или ссылку.")
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
                return

            if text.lower() == "/clear":
                data = load_data()
                cnt = len(data["channels"])
                data["channels"] = []
                save_data(data)
                await event.reply(f"Удалено каналов: {cnt}")
                return

            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("/"):
                    continue

                ref = line
                m = re.search(r'@(\w{5,})', line)
                if m:
                    ref = m.group(0)
                else:
                    m = re.search(
                        r'https?://t\.me/(\+[\w-]+|joinchat/[\w-]+|\w{5,})', line,
                    )
                    if m:
                        full = m.group(0)
                        if "/+" in full or "/joinchat/" in full:
                            ref = full
                        else:
                            ref = f"@{m.group(1)}"

                try:
                    entity = await user.get_entity(ref)
                    ch_id = entity.id
                    ch_title = getattr(entity, "title", ref)
                except Exception as e:
                    await event.reply(f"❌ {ref}: {e}")
                    continue

                data = load_data()
                if ch_id in data["channels"]:
                    await event.reply(f"⚠️ Уже есть: {ch_title}")
                    continue

                data["channels"].append(ch_id)
                save_data(data)
                await event.reply(
                    f"✅ {ch_title}",
                    buttons=[Button.inline(
                        "❌ Убрать", data=f"rc{ch_id}".encode(),
                    )],
                )

        # ── топик «Позиции» ────────────────────────────────────────────
        elif tid == cfg["topic_positions"]:
            if text.lower() == "/list":
                data = load_data()
                if not data["positions"]:
                    await event.reply("Пусто. Отправь ключевые слова через запятую.")
                    return
                lines = [
                    f"{i + 1}. {', '.join(p)}"
                    for i, p in enumerate(data["positions"])
                ]
                await event.reply("📋 Позиции:\n" + "\n".join(lines))
                return

            if text.lower() == "/clear":
                data = load_data()
                cnt = len(data["positions"])
                data["positions"] = []
                save_data(data)
                await event.reply(f"Удалено позиций: {cnt}")
                return

            added = 0
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("/"):
                    continue
                keywords = [k.strip() for k in line.split(",") if k.strip()]
                if not keywords:
                    continue

                data = load_data()
                norm_new = [norm(k) for k in keywords]
                if any([norm(k) for k in p] == norm_new for p in data["positions"]):
                    await event.reply(f"⚠️ Уже есть: {', '.join(keywords)}")
                    continue

                data["positions"].append(keywords)
                save_data(data)
                added += 1

                ph = pos_hash(keywords)
                await event.reply(
                    f"✅ {', '.join(keywords)}",
                    buttons=[Button.inline(
                        "❌ Убрать", data=f"rp{ph}".encode(),
                    )],
                )

            if added:
                data = load_data()
                await event.reply(f"Всего позиций: {len(data['positions'])}")

    # ── файлы с позициями ─────────────────────────────────────────────────

    @bot.on(events.NewMessage(
        func=lambda e: (
            e.is_group
            and e.document is not None
            and e.sender_id == ADMIN_ID
            and e.chat_id == cfg.get("group_id")
        ),
    ))
    async def on_group_file(event):
        tid = get_topic_id(event.message)
        if tid != cfg["topic_positions"]:
            return

        name = ""
        for attr in event.document.attributes:
            if hasattr(attr, "file_name"):
                name = attr.file_name
                break
        if not name.endswith((".csv", ".json", ".txt")):
            return

        raw = await event.download_media(bytes)
        content = raw.decode("utf-8-sig")
        data = load_data()
        added = 0

        if name.endswith(".json"):
            for item in json.loads(content):
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
            for row in csv.reader(io.StringIO(content)):
                kw = [c.strip() for c in row if c.strip()]
                if kw:
                    data["positions"].append(kw)
                    added += 1
        elif name.endswith(".txt"):
            for line_text in content.splitlines():
                line_text = line_text.strip()
                if line_text:
                    kw = [k.strip() for k in line_text.split(",") if k.strip()]
                    data["positions"].append(kw)
                    added += 1

        save_data(data)
        await event.reply(f"✅ Загружено: {added}\nВсего: {len(data['positions'])}")

    # ── кнопки удаления ───────────────────────────────────────────────────

    @bot.on(events.CallbackQuery(pattern=rb"rc(\d+)"))
    async def cb_rm_channel(event):
        if event.sender_id != ADMIN_ID:
            await event.answer("Нет доступа")
            return
        ch_id = int(event.pattern_match.group(1))
        data = load_data()
        if ch_id in data["channels"]:
            data["channels"].remove(ch_id)
            save_data(data)
            await event.edit("❌ Канал убран")
        else:
            await event.answer("Уже убран")

    @bot.on(events.CallbackQuery(pattern=rb"rp(\w+)"))
    async def cb_rm_position(event):
        if event.sender_id != ADMIN_ID:
            await event.answer("Нет доступа")
            return
        ph = event.pattern_match.group(1).decode()
        data = load_data()
        for i, pos in enumerate(data["positions"]):
            if pos_hash(pos) == ph:
                removed = data["positions"].pop(i)
                save_data(data)
                await event.edit(f"❌ Убрано: {', '.join(removed)}")
                return
        await event.answer("Уже убрано")

    # ── кнопки копирования ────────────────────────────────────────────────

    @bot.on(events.CallbackQuery(data=b"copy_msg"))
    async def cb_copy_msg(event):
        cached = _copy_cache.get(event.message_id)
        if cached:
            await bot.send_message(
                event.chat_id, cached["msg"],
                reply_to=event.message_id,
            )
        else:
            await event.answer("Устарело")

    @bot.on(events.CallbackQuery(data=b"copy_pos"))
    async def cb_copy_pos(event):
        cached = _copy_cache.get(event.message_id)
        if cached:
            await bot.send_message(
                event.chat_id, cached["pos"],
                reply_to=event.message_id,
            )
        else:
            await event.answer("Устарело")

    # ── мониторинг каналов (юзербот) ──────────────────────────────────────

    @user.on(events.NewMessage)
    async def on_channel_message(event):
        if not event.is_channel and not event.is_group:
            return
        if not cfg["group_id"] or not cfg["topic_requests"]:
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
        if chat.id == cfg["group_id"]:
            return

        matched = match_positions(msg_text, data["positions"])
        if not matched:
            return

        ch_title = getattr(chat, "title", str(chat.id))
        ch_username = getattr(chat, "username", None)
        if ch_username:
            msg_link = f"https://t.me/{ch_username}/{event.id}"
        else:
            msg_link = f"https://t.me/c/{chat.id}/{event.id}"

        sender = await event.get_sender()
        if isinstance(sender, User):
            name = (
                f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                or "—"
            )
            if sender.username:
                buyer_line = (
                    f"{name} (@{sender.username})\n"
                    f"https://t.me/{sender.username}"
                )
            else:
                buyer_line = f"{name} (tg://user?id={sender.id})"
        elif sender and hasattr(sender, "title"):
            buyer_line = sender.title
        else:
            buyer_line = "Аноним (пост канала)"

        pos_str = "\n".join(f"  • {', '.join(m)}" for m in matched)
        display = msg_text[:800] + ("..." if len(msg_text) > 800 else "")

        notification = (
            f"📢 Совпадение!\n\n"
            f"Канал: {ch_title}\n\n"
            f"Ссылка: {msg_link}\n\n"
            f"Покупатель: {buyer_line}\n\n"
            f"Сообщение:\n{display}\n\n"
            f"Позиция:\n{pos_str}"
        )

        buttons = [[
            Button.inline("📋 Сообщение", data=b"copy_msg"),
            Button.inline("📋 Позиция", data=b"copy_pos"),
        ]]

        try:
            sent = await bot.send_message(
                cfg["group_id"],
                notification,
                reply_to=cfg["topic_requests"],
                buttons=buttons,
                link_preview=False,
            )
            _copy_cache[sent.id] = {"msg": msg_text, "pos": pos_str}
            if len(_copy_cache) > 500:
                for k in sorted(_copy_cache)[:250]:
                    del _copy_cache[k]
        except Exception:
            log.exception("Ошибка отправки уведомления")

    # ── старт ─────────────────────────────────────────────────────────────

    data = load_data()
    log.info("Мониторинг запущен. Каналов: %d, позиций: %d",
             len(data["channels"]), len(data["positions"]))

    if not cfg["group_id"]:
        log.warning("Группа не настроена. Добавь бота в группу и отправь /setup")

    await asyncio.gather(
        user.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        sys.exit("Нужны API_ID и API_HASH")
    if not BOT_TOKEN:
        sys.exit("Нужен BOT_TOKEN")
    if not ADMIN_ID:
        sys.exit("Нужен ADMIN_ID")
    asyncio.run(main())
