#!/usr/bin/env python3
"""
Мониторинг Telegram-каналов с матчингом по позициям.

Управление через форум-группу с топиками:
  📥 Запросы — уведомления о совпадениях
  📡 Каналы — ID каналов для мониторинга
  📦 Позиции — позиции для поиска (ключевые слова через запятую)
  🏷 Теги — ответы бота (подтверждения, ошибки, списки)

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


cfg = {
    "group_id": 0,
    "topic_requests": 0,
    "topic_channels": 0,
    "topic_positions": 0,
    "topic_tags": 0,
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

_CYR = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e',
    'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k',
    'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
    'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya',
}

_SOFT_UNITS = {'gb', 'tb', 'mp'}
_UNIT_TAIL = re.compile(r'(gb|tb|mp)$')

_COLOR_GROUPS = [
    {'black', 'chernyy'},
    {'white', 'belyy'},
    {'blue', 'siniy', 'goluboy'},
    {'red', 'krasnyy'},
    {'green', 'zelenyy'},
    {'gold', 'zolotoy'},
    {'silver', 'serebristyy', 'serebryanyy'},
    {'pink', 'rozovyy'},
    {'purple', 'fioletovyy'},
    {'gray', 'grey', 'seryy'},
    {'yellow', 'zheltyy'},
    {'orange', 'oranzhevyy'},
    {'titanium', 'titanovyy'},
    {'graphite', 'grafitovyy'},
    {'coral', 'korallovyy'},
    {'cream', 'kremovyy'},
    {'lavender', 'lavandovyy'},
    {'mint', 'myatnyy'},
]
_COLOR_LOOKUP: dict[str, set[str]] = {}
for _grp in _COLOR_GROUPS:
    for _f in _grp:
        _COLOR_LOOKUP[_f] = _grp


def norm(text: str) -> str:
    t = text.strip().lower()
    out = []
    for ch in t:
        out.append(_CYR.get(ch, ch))
    result = ''.join(out)
    result = re.sub(r'(\d)\s*(gb|tb|mp)\b', r'\1\2', result)
    return re.sub(r'\s+', ' ', result)


def _color_in_text(keyword_normed: str, text_normed: str) -> bool:
    for word in keyword_normed.split():
        group = _COLOR_LOOKUP.get(word)
        if group:
            return any(form in text_normed for form in group)
    return False


def match_positions(text: str, positions: list) -> list:
    n = norm(text)
    n_ns = n.replace(' ', '')
    matched = []
    for pos in positions:
        keywords = pos if isinstance(pos, list) else [pos]
        ok = True
        for kw in keywords:
            nkw = norm(kw)
            if nkw in _SOFT_UNITS:
                continue
            if nkw in n or nkw.replace(' ', '') in n_ns:
                continue
            stripped = _UNIT_TAIL.sub('', nkw)
            if stripped and stripped != nkw:
                if stripped in n or stripped in n_ns:
                    continue
            if _color_in_text(nkw, n):
                continue
            ok = False
            break
        if ok:
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
    topics = {}
    try:
        async for msg in client.iter_messages(group_id, limit=300):
            if not hasattr(msg, "action") or not msg.action:
                continue
            action_name = type(msg.action).__name__
            if "TopicCreate" not in action_name:
                continue
            title = getattr(msg.action, "title", "")
            name = title.lower().strip()
            if "запрос" in name:
                topics["requests"] = msg.id
            elif "канал" in name:
                topics["channels"] = msg.id
            elif "тег" in name:
                topics["tags"] = msg.id
            elif "позиц" in name:
                topics["positions"] = msg.id
    except Exception as e:
        log.error("Ошибка сканирования топиков: %s", e)
    return topics


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
        cfg["topic_requests"] = saved.get("topic_requests", 0)
        cfg["topic_channels"] = saved.get("topic_channels", 0)
        cfg["topic_positions"] = saved.get("topic_positions", 0)
        cfg["topic_tags"] = saved.get("topic_tags", 0)

        # Если топики не сохранены — автодетект
        if not all([cfg["topic_requests"], cfg["topic_channels"],
                    cfg["topic_positions"], cfg["topic_tags"]]):
            topics = await detect_topics(bot, group_id)
            if not topics:
                topics = await detect_topics(user, group_id)
            cfg["topic_requests"] = cfg["topic_requests"] or topics.get("requests", 0)
            cfg["topic_channels"] = cfg["topic_channels"] or topics.get("channels", 0)
            cfg["topic_positions"] = cfg["topic_positions"] or topics.get("positions", 0)
            cfg["topic_tags"] = cfg["topic_tags"] or topics.get("tags", 0)

        if all([cfg["topic_requests"], cfg["topic_channels"],
                cfg["topic_positions"], cfg["topic_tags"]]):
            log.info("Группа: %d | Запросы: %d | Каналы: %d | Позиции: %d | Теги: %d",
                     cfg["group_id"], cfg["topic_requests"],
                     cfg["topic_channels"], cfg["topic_positions"],
                     cfg["topic_tags"])
        else:
            log.warning("Не все топики найдены. Отправь /setup в группе.")

    # ── /setup ─────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/setup", from_users=ADMIN_ID))
    async def cmd_setup(event):
        if not event.is_group:
            await event.reply(
                "Настройка — в группе с топиками.\n\n"
                "Отправь в каждом топике:\n"
                "/setup запросы — в топике для уведомлений\n"
                "/setup каналы — в топике для каналов\n"
                "/setup позиции — в топике для позиций\n"
                "/setup теги — в топике для ответов бота"
            )
            return

        gid = event.chat_id
        arg = event.text.partition(" ")[2].strip().lower()
        tid = get_topic_id(event.message)

        # /setup без аргумента — попробовать автодетект
        if not arg:
            topics = await detect_topics(bot, gid)
            if not topics:
                topics = await detect_topics(user, gid)

            if all(k in topics for k in ("requests", "channels", "positions", "tags")):
                cfg["group_id"] = gid
                cfg["topic_requests"] = topics["requests"]
                cfg["topic_channels"] = topics["channels"]
                cfg["topic_positions"] = topics["positions"]
                cfg["topic_tags"] = topics["tags"]
                save_config({
                    "group_id": gid,
                    "topic_requests": topics["requests"],
                    "topic_channels": topics["channels"],
                    "topic_positions": topics["positions"],
                    "topic_tags": topics["tags"],
                })
                await event.reply(
                    f"✅ Автонастройка!\n\n"
                    f"📥 Запросы → {topics['requests']}\n"
                    f"📡 Каналы → {topics['channels']}\n"
                    f"📦 Позиции → {topics['positions']}\n"
                    f"🏷 Теги → {topics['tags']}"
                )
                log.info("Auto-setup: group=%d r=%d c=%d p=%d t=%d",
                         gid, topics["requests"], topics["channels"],
                         topics["positions"], topics["tags"])
                return

            await event.reply(
                "Автодетект не нашёл все топики.\n\n"
                "Отправь в каждом топике:\n"
                "/setup запросы\n"
                "/setup каналы\n"
                "/setup позиции\n"
                "/setup теги"
            )
            return

        # /setup <тип> — ручная привязка топика
        if not tid:
            await event.reply("Отправь эту команду внутри топика, не в General.")
            return

        cfg["group_id"] = gid

        if "запрос" in arg:
            cfg["topic_requests"] = tid
            await event.reply("✅ Топик «Запросы» настроен")
        elif "канал" in arg:
            cfg["topic_channels"] = tid
            await event.reply("✅ Топик «Каналы» настроен")
        elif "позиц" in arg:
            cfg["topic_positions"] = tid
            await event.reply("✅ Топик «Позиции» настроен")
        elif "тег" in arg:
            cfg["topic_tags"] = tid
            await event.reply("✅ Топик «Теги» настроен")
        else:
            await event.reply(
                "Укажи тип:\n"
                "/setup запросы\n"
                "/setup каналы\n"
                "/setup позиции\n"
                "/setup теги"
            )
            return

        save_config({
            "group_id": gid,
            "topic_requests": cfg["topic_requests"],
            "topic_channels": cfg["topic_channels"],
            "topic_positions": cfg["topic_positions"],
            "topic_tags": cfg["topic_tags"],
        })

        if all([cfg["topic_requests"], cfg["topic_channels"],
                cfg["topic_positions"], cfg["topic_tags"]]):
            await event.reply("🎉 Все топики настроены! Бот готов к работе.")
            log.info("Manual setup done: group=%d r=%d c=%d p=%d t=%d",
                     gid, cfg["topic_requests"], cfg["topic_channels"],
                     cfg["topic_positions"], cfg["topic_tags"])

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

        tags = cfg["topic_tags"]

        async def to_tags(text, **kw):
            if tags:
                await bot.send_message(cfg["group_id"], text, reply_to=tags, **kw)
            else:
                await event.reply(text, **kw)

        # ── топик «Каналы» ─────────────────────────────────────────────
        if tid == cfg["topic_channels"]:
            if text.lower() == "/list":
                data = load_data()
                if not data["channels"]:
                    await to_tags("Пусто. Отправь ID канала (число).")
                    return
                lines = []
                for ch_id in data["channels"]:
                    try:
                        entity = await user.get_entity(ch_id)
                        title = getattr(entity, "title", str(ch_id))
                        lines.append(f"• {title} ({ch_id})")
                    except Exception:
                        lines.append(f"• {ch_id}")
                await to_tags("📡 Каналы:\n" + "\n".join(lines))
                return

            if text.lower() == "/clear":
                data = load_data()
                cnt = len(data["channels"])
                data["channels"] = []
                save_data(data)
                await to_tags(f"Удалено каналов: {cnt}")
                return

            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("/"):
                    continue

                digits = re.sub(r"[^\d]", "", line)
                if not digits:
                    await to_tags(f"❌ Не число: {line}")
                    continue
                ch_id = int(digits)

                data = load_data()
                if ch_id in data["channels"]:
                    await to_tags(f"⚠️ Уже есть: {ch_id}")
                    continue

                data["channels"].append(ch_id)
                save_data(data)

                try:
                    entity = await user.get_entity(ch_id)
                    ch_title = getattr(entity, "title", str(ch_id))
                except Exception:
                    ch_title = str(ch_id)

                await to_tags(
                    f"✅ {ch_title} ({ch_id})",
                    buttons=[Button.inline(
                        "❌ Убрать", data=f"rc{ch_id}".encode(),
                    )],
                )

        # ── топик «Позиции» ────────────────────────────────────────────
        elif tid == cfg["topic_positions"]:
            if text.lower() == "/list":
                data = load_data()
                if not data["positions"]:
                    await to_tags("Пусто. Отправь ключевые слова через запятую.")
                    return
                lines = [
                    f"{i + 1}. {', '.join(p)}"
                    for i, p in enumerate(data["positions"])
                ]
                await to_tags("📋 Позиции:\n" + "\n".join(lines))
                return

            if text.lower() == "/clear":
                data = load_data()
                cnt = len(data["positions"])
                data["positions"] = []
                save_data(data)
                await to_tags(f"Удалено позиций: {cnt}")
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
                    await to_tags(f"⚠️ Уже есть: {', '.join(keywords)}")
                    continue

                data["positions"].append(keywords)
                save_data(data)
                added += 1

                ph = pos_hash(keywords)
                await to_tags(
                    f"✅ {', '.join(keywords)}",
                    buttons=[Button.inline(
                        "❌ Убрать", data=f"rp{ph}".encode(),
                    )],
                )

            if added:
                data = load_data()
                await to_tags(f"Всего позиций: {len(data['positions'])}")

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
        tags = cfg["topic_tags"]
        msg = f"✅ Загружено: {added}\nВсего: {len(data['positions'])}"
        if tags:
            await bot.send_message(cfg["group_id"], msg, reply_to=tags)
        else:
            await event.reply(msg)

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
        buyer_url = None
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
                buyer_url = f"https://t.me/{sender.username}"
            else:
                buyer_line = f"{name} (tg://user?id={sender.id})"
                buyer_url = f"tg://user?id={sender.id}"
        elif sender and hasattr(sender, "title"):
            buyer_line = sender.title
        else:
            buyer_line = "Аноним (пост канала)"

        pos_str = "\n".join(f"`{', '.join(m)}`" for m in matched)
        display = msg_text[:800] + ("..." if len(msg_text) > 800 else "")

        notification = (
            f"📢 Совпадение!\n\n"
            f"Канал: {ch_title}\n\n"
            f"Ссылка: {msg_link}\n\n"
            f"Покупатель: {buyer_line}\n\n"
            f"Сообщение:\n{display}\n\n"
            f"Позиция:\n{pos_str}"
        )

        buttons = []
        if buyer_url:
            buttons.append([Button.url("💬 Чат", buyer_url)])

        try:
            await bot.send_message(
                cfg["group_id"],
                notification,
                reply_to=cfg["topic_requests"],
                buttons=buttons or None,
                link_preview=False,
                parse_mode="md",
            )
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
