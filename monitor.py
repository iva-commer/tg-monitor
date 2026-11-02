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
from datetime import timezone, timedelta
from pathlib import Path

from aiohttp import web as aio_web
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
_extra = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {ADMIN_ID} | {int(x) for x in _extra.split(",") if x.strip().isdigit()}

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WEBAPP_DIR = Path(__file__).parent / "webapp"

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "data.json"
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
SESSION_DIR = Path(__file__).parent / "sessions"


cfg = {
    "group_id": 0,
    "topic_requests": 0,
    "topic_channels": 0,
    "topic_positions": 0,
    "topic_tags": 0,
    "topic_analytics": 0,
}


# ── хранилище ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text("utf-8"))
        migrated = False
        for i, pos in enumerate(data.get("positions", [])):
            if isinstance(pos, list):
                data["positions"][i] = {"kw": pos, "pr": "", "on": True}
                migrated = True
        for pos in data.get("positions", []):
            if not pos.get("pr") and len(pos["kw"]) == 1:
                m = re.search(r'\s+[—–\-]?\s*(\d[\d.\s]*\d|\d{4,})$', pos["kw"][0])
                if m:
                    pos["pr"] = m.group(1).replace(" ", "").replace(".", "")
                    pos["kw"][0] = pos["kw"][0][:m.start()]
                    migrated = True
        if migrated:
            save_data(data)
        return data
    return {"positions": [], "channels": []}


def save_data(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text("utf-8"))
    return {}


def save_stats(stats: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")


def record_match(channel_id: int, pos_hash_val: str) -> None:
    MSK = timezone(timedelta(hours=3))
    from datetime import datetime
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    stats = load_stats()
    day = stats.setdefault(today, {})
    ch = day.setdefault(str(channel_id), {})
    ch[pos_hash_val] = ch.get(pos_hash_val, 0) + 1
    save_stats(stats)


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

_MODEL_MODIFIERS = {'ultra', 'plus', 'pro', 'max', 'lite', 'mini', 'note', 'fe'}

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


_COLOR_PREFIXES = {
    'jet', 'ice', 'midnight', 'space', 'sierra', 'desert', 'natural',
    'starlight', 'deep', 'dark', 'light', 'pale', 'bright', 'neon',
    'royal', 'product',
}


def _split_position(keywords: list) -> list:
    result = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        words = kw.split()
        if len(words) == 1:
            result.append(kw)
            continue
        i = 0
        while i < len(words):
            nw = norm(words[i])
            if (i + 1 < len(words)
                    and norm(words[i + 1]) in _COLOR_LOOKUP
                    and (nw in _COLOR_PREFIXES or nw in _COLOR_LOOKUP)):
                result.append(f"{words[i]} {words[i + 1]}")
                i += 2
            else:
                result.append(words[i])
                i += 1
    return result


def _flexi_pattern(token: str) -> str:
    parts = re.split(r'(\d+)', token)
    return r'\s*'.join(re.escape(p) for p in parts if p)


def _word_match(token: str, text: str) -> bool:
    pat = _flexi_pattern(token)
    return bool(re.search(r'\b' + pat + r'\b', text))


def _kw_in_segment(keywords: list, seg: str) -> bool:
    for kw in keywords:
        nkw = norm(kw)
        if nkw in _SOFT_UNITS:
            continue
        if _word_match(nkw, seg):
            continue
        no_z = nkw.replace(' ', '').lstrip('z')
        if no_z != nkw.replace(' ', '') and no_z and _word_match(no_z, seg):
            continue
        stripped = _UNIT_TAIL.sub('', nkw)
        if stripped and stripped != nkw and _word_match(stripped, seg):
            continue
        if _color_in_text(nkw, seg):
            continue
        return False
    kw_norms = {norm(kw) for kw in keywords}
    msg_words = set(seg.split())
    for mod in _MODEL_MODIFIERS:
        if mod in msg_words and mod not in kw_norms:
            return False
    return True


def match_positions(text: str, positions: list) -> list:
    segments = [norm(line) for line in text.split('\n') if line.strip()]
    if not segments:
        return []
    matched = []
    for pos in positions:
        if isinstance(pos, dict):
            if not pos.get("on", True):
                continue
            raw = pos["kw"]
        else:
            raw = pos if isinstance(pos, list) else [pos]
        keywords = _split_position(raw)
        if not keywords:
            continue
        if any(_kw_in_segment(keywords, seg) for seg in segments):
            matched.append(pos)
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
            elif "аналитик" in name:
                topics["analytics"] = msg.id
    except Exception as e:
        log.error("Ошибка сканирования топиков: %s", e)
    return topics


# ── web API ───────────────────────────────────────────────────────────────

async def _api_data(request):
    d = load_data()
    return aio_web.json_response({
        "positions": d.get("positions", []),
        "channels": d.get("channels", []),
    })


async def _api_stats(request):
    from datetime import datetime
    date = request.query.get("date", "")
    if not date:
        MSK = timezone(timedelta(hours=3))
        date = datetime.now(MSK).strftime("%Y-%m-%d")
    stats = load_stats()
    day = stats.get(date, {})
    d = load_data()
    names = {}
    for pos in d["positions"]:
        if isinstance(pos, dict):
            names[pos_hash(pos["kw"])] = ", ".join(pos["kw"])
    return aio_web.json_response({
        "date": date,
        "channels": day,
        "position_names": names,
    })


async def _api_toggle(request):
    body = await request.json()
    idx = body.get("index", -1)
    d = load_data()
    if 0 <= idx < len(d["positions"]):
        pos = d["positions"][idx]
        pos["on"] = not pos.get("on", True)
        save_data(d)
    return aio_web.json_response({"ok": True})


async def _api_delete(request):
    body = await request.json()
    idx = body.get("index", -1)
    d = load_data()
    if 0 <= idx < len(d["positions"]):
        d["positions"].pop(idx)
        save_data(d)
    return aio_web.json_response({"ok": True})


async def _api_add(request):
    body = await request.json()
    text = body.get("text", "")
    d = load_data()
    added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        keywords = [k.strip() for k in line.split(",") if k.strip()]
        keywords = [k for k in keywords if k not in ('—', '–', '-')]
        if not keywords:
            continue
        price = ""
        if keywords:
            last = keywords[-1].lstrip('—–- ').strip()
            if re.fullmatch(r'\d[\d\s.]*', last):
                price = last.replace(" ", "").replace(".", "")
                keywords.pop()
        if not price and len(keywords) == 1:
            m_pr = re.search(r'\s+[—–\-]?\s*(\d[\d.\s]*\d|\d{4,})$', keywords[0])
            if m_pr:
                price = m_pr.group(1).replace(" ", "").replace(".", "")
                keywords[0] = keywords[0][:m_pr.start()]
        if not keywords or not keywords[0]:
            continue
        norm_new = [norm(k) for k in keywords]
        if any([norm(k) for k in p["kw"]] == norm_new for p in d["positions"]):
            continue
        d["positions"].append({"kw": keywords, "pr": price, "on": True})
        added += 1
    if added:
        save_data(d)
    return aio_web.json_response({"added": added})


async def _webapp_index(request):
    return aio_web.FileResponse(WEBAPP_DIR / "index.html")


def _setup_web_app():
    app = aio_web.Application()
    app.router.add_get('/api/data', _api_data)
    app.router.add_get('/api/stats', _api_stats)
    app.router.add_post('/api/toggle', _api_toggle)
    app.router.add_post('/api/delete', _api_delete)
    app.router.add_post('/api/add', _api_add)
    app.router.add_get('/', _webapp_index)
    app.router.add_get('/app', _webapp_index)
    app.router.add_get('/app/', _webapp_index)
    return app


# ── основной цикл ─────────────────────────────────────────────────────────

async def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    user = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)
    bot = TelegramClient(str(SESSION_DIR / "bot"), API_ID, API_HASH)

    await user.start(phone=os.getenv("PHONE", ""))
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Юзербот и бот запущены. ADMIN_IDS=%s", ADMIN_IDS)

    # ── загрузка конфига ──────────────────────────────────────────────────

    saved = load_config()
    group_id = int(os.getenv("GROUP_ID", "0")) or saved.get("group_id", 0)

    if group_id:
        cfg["group_id"] = group_id
        cfg["topic_requests"] = saved.get("topic_requests", 0)
        cfg["topic_channels"] = saved.get("topic_channels", 0)
        cfg["topic_positions"] = saved.get("topic_positions", 0)
        cfg["topic_tags"] = saved.get("topic_tags", 0)
        cfg["topic_analytics"] = saved.get("topic_analytics", 0)

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
            cfg["topic_analytics"] = cfg["topic_analytics"] or topics.get("analytics", 0)

        if all([cfg["topic_requests"], cfg["topic_channels"],
                cfg["topic_positions"], cfg["topic_tags"]]):
            log.info("Группа: %d | Запросы: %d | Каналы: %d | Позиции: %d | Теги: %d",
                     cfg["group_id"], cfg["topic_requests"],
                     cfg["topic_channels"], cfg["topic_positions"],
                     cfg["topic_tags"])
        else:
            log.warning("Не все топики найдены. Отправь /setup в группе.")

    # ── /setup ─────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/setup", from_users=list(ADMIN_IDS)))
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
                cfg["topic_analytics"] = topics.get("analytics", 0)
                save_config({
                    "group_id": gid,
                    "topic_requests": topics["requests"],
                    "topic_channels": topics["channels"],
                    "topic_positions": topics["positions"],
                    "topic_tags": topics["tags"],
                    "topic_analytics": cfg["topic_analytics"],
                })
                analytics_line = f"\n📊 Аналитика → {cfg['topic_analytics']}" if cfg["topic_analytics"] else ""
                await event.reply(
                    f"✅ Автонастройка!\n\n"
                    f"📥 Запросы → {topics['requests']}\n"
                    f"📡 Каналы → {topics['channels']}\n"
                    f"📦 Позиции → {topics['positions']}\n"
                    f"🏷 Теги → {topics['tags']}"
                    f"{analytics_line}"
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
        elif "аналитик" in arg:
            cfg["topic_analytics"] = tid
            await event.reply("✅ Топик «Аналитика» настроен")
        else:
            await event.reply(
                "Укажи тип:\n"
                "/setup запросы\n"
                "/setup каналы\n"
                "/setup позиции\n"
                "/setup теги\n"
                "/setup аналитика"
            )
            return

        save_config({
            "group_id": gid,
            "topic_requests": cfg["topic_requests"],
            "topic_channels": cfg["topic_channels"],
            "topic_positions": cfg["topic_positions"],
            "topic_tags": cfg["topic_tags"],
            "topic_analytics": cfg["topic_analytics"],
        })

        if all([cfg["topic_requests"], cfg["topic_channels"],
                cfg["topic_positions"], cfg["topic_tags"]]):
            await event.reply("🎉 Все топики настроены! Бот готов к работе.")
            log.info("Manual setup done: group=%d r=%d c=%d p=%d t=%d",
                     gid, cfg["topic_requests"], cfg["topic_channels"],
                     cfg["topic_positions"], cfg["topic_tags"])

    # ── /start в личке ────────────────────────────────────────────────────

    @bot.on(events.NewMessage(
        pattern="/start", from_users=list(ADMIN_IDS),
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

    @bot.on(events.NewMessage(func=lambda e: e.is_group and e.sender_id in ADMIN_IDS))
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
                lines = []
                for i, p in enumerate(data["positions"]):
                    status = "▶️" if p.get("on", True) else "⏸"
                    kw_str = ', '.join(p["kw"])
                    price_str = f"  — {p['pr']}₽" if p.get("pr") else ""
                    lines.append(f"{status} {i + 1}. {kw_str}{price_str}")
                buttons = []
                for i, p in enumerate(data["positions"]):
                    row = []
                    if p.get("on", True):
                        row.append(Button.inline(f"⏸ {i+1}", data=f"off{i}".encode()))
                    else:
                        row.append(Button.inline(f"▶️ {i+1}", data=f"on{i}".encode()))
                    ph = pos_hash(p["kw"])
                    row.append(Button.inline(f"❌ {i+1}", data=f"rp{ph}".encode()))
                    buttons.append(row)
                await to_tags(
                    "📋 Позиции:\n" + "\n".join(lines),
                    buttons=buttons,
                )
                return

            cmd_lower = text.lower().split()
            if cmd_lower and cmd_lower[0] == "/on" and len(cmd_lower) >= 2 and cmd_lower[1].isdigit():
                idx = int(cmd_lower[1]) - 1
                data = load_data()
                if 0 <= idx < len(data["positions"]):
                    data["positions"][idx]["on"] = True
                    save_data(data)
                    kw_str = ', '.join(data["positions"][idx]["kw"])
                    await to_tags(f"▶️ Включена: {kw_str}")
                else:
                    await to_tags("❌ Неверный номер")
                return

            if cmd_lower and cmd_lower[0] == "/off" and len(cmd_lower) >= 2 and cmd_lower[1].isdigit():
                idx = int(cmd_lower[1]) - 1
                data = load_data()
                if 0 <= idx < len(data["positions"]):
                    data["positions"][idx]["on"] = False
                    save_data(data)
                    kw_str = ', '.join(data["positions"][idx]["kw"])
                    await to_tags(f"⏸ Выключена: {kw_str}")
                else:
                    await to_tags("❌ Неверный номер")
                return

            if cmd_lower and cmd_lower[0] == "/price" and len(cmd_lower) >= 3 and cmd_lower[1].isdigit():
                idx = int(cmd_lower[1]) - 1
                new_price = cmd_lower[2].replace(" ", "")
                data = load_data()
                if 0 <= idx < len(data["positions"]):
                    data["positions"][idx]["pr"] = new_price
                    save_data(data)
                    kw_str = ', '.join(data["positions"][idx]["kw"])
                    await to_tags(f"💰 {kw_str} — {new_price}₽")
                else:
                    await to_tags("❌ Неверный номер")
                return

            if text.lower() == "/clear":
                data = load_data()
                cnt = len(data["positions"])
                data["positions"] = []
                save_data(data)
                await to_tags(f"Удалено позиций: {cnt}")
                return

            added_items = []
            dupes = []
            data = load_data()
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("/"):
                    continue
                keywords = [k.strip() for k in line.split(",") if k.strip()]
                keywords = [k for k in keywords if k not in ('—', '–', '-')]
                if not keywords:
                    continue

                price = ""
                if keywords:
                    last = keywords[-1].lstrip('—–- ').strip()
                    if re.fullmatch(r'\d[\d\s.]*', last):
                        price = last.replace(" ", "").replace(".", "")
                        keywords.pop()
                if not price and len(keywords) == 1:
                    m_pr = re.search(r'\s+[—–\-]?\s*(\d[\d.\s]*\d|\d{4,})$', keywords[0])
                    if m_pr:
                        price = m_pr.group(1).replace(" ", "").replace(".", "")
                        keywords[0] = keywords[0][:m_pr.start()]
                if not keywords or not keywords[0]:
                    continue

                norm_new = [norm(k) for k in keywords]
                if any(
                    [norm(k) for k in p["kw"]] == norm_new
                    for p in data["positions"]
                ):
                    dupes.append(', '.join(keywords))
                    continue

                entry = {"kw": keywords, "pr": price, "on": True}
                data["positions"].append(entry)
                added_items.append(entry)

            if added_items:
                save_data(data)
                lines = []
                buttons = []
                for entry in added_items:
                    label = ', '.join(entry["kw"])
                    if entry["pr"]:
                        label += f" — {entry['pr']}₽"
                    lines.append(f"✅ {label}")
                    ph = pos_hash(entry["kw"])
                    buttons.append([Button.inline(
                        f"❌ {', '.join(entry['kw'])[:25]}",
                        data=f"rp{ph}".encode(),
                    )])
                msg = "\n".join(lines) + f"\n\nВсего позиций: {len(data['positions'])}"
                if dupes:
                    msg += "\n\n⚠️ Уже были: " + " | ".join(dupes)
                await to_tags(msg, buttons=buttons)
            elif dupes:
                await to_tags("⚠️ Уже есть: " + " | ".join(dupes))

        # ── топик «Аналитика» ──────────────────────────────────────────
        elif tid == cfg.get("topic_analytics") and cfg.get("topic_analytics"):
            if text.lower() in ("/report", "/отчет"):
                from datetime import datetime
                MSK = timezone(timedelta(hours=3))
                today = datetime.now(MSK).strftime("%Y-%m-%d")
                stats = load_stats()
                day_stats = stats.get(today, {})

                if not day_stats:
                    await to_tags(f"📊 Отчёт за {today}\n\nНет данных за сегодня.")
                    return

                data = load_data()
                pos_map = {}
                for p in data["positions"]:
                    ph = pos_hash(p["kw"])
                    label = ', '.join(p["kw"])
                    if p.get("pr"):
                        label += f" — {p['pr']}₽"
                    pos_map[ph] = label

                report_lines = [f"📊 Отчёт за {today}\n"]
                for ch_id_str, positions in day_stats.items():
                    try:
                        entity = await user.get_entity(int(ch_id_str))
                        ch_name = getattr(entity, "title", ch_id_str)
                    except Exception:
                        ch_name = ch_id_str
                    report_lines.append(f"📡 {ch_name}:")
                    for ph_val, count in sorted(positions.items(), key=lambda x: -x[1]):
                        pos_label = pos_map.get(ph_val, ph_val)
                        report_lines.append(f"  • {pos_label} — {count} запр.")
                    report_lines.append("")

                await to_tags("\n".join(report_lines))
                return

    # ── файлы с позициями ─────────────────────────────────────────────────

    @bot.on(events.NewMessage(
        func=lambda e: (
            e.is_group
            and e.document is not None
            and e.sender_id in ADMIN_IDS
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
                    data["positions"].append({"kw": kw, "pr": "", "on": True})
                    added += 1
        elif name.endswith(".csv"):
            for row in csv.reader(io.StringIO(content)):
                kw = [c.strip() for c in row if c.strip()]
                if kw:
                    data["positions"].append({"kw": kw, "pr": "", "on": True})
                    added += 1
        elif name.endswith(".txt"):
            for line_text in content.splitlines():
                line_text = line_text.strip()
                if line_text:
                    kw = [k.strip() for k in line_text.split(",") if k.strip()]
                    data["positions"].append({"kw": kw, "pr": "", "on": True})
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
        if event.sender_id not in ADMIN_IDS:
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
        if event.sender_id not in ADMIN_IDS:
            await event.answer("Нет доступа")
            return
        ph = event.pattern_match.group(1).decode()
        data = load_data()
        for i, pos in enumerate(data["positions"]):
            if pos_hash(pos["kw"]) == ph:
                removed = data["positions"].pop(i)
                save_data(data)
                await event.edit(f"❌ Убрано: {', '.join(removed['kw'])}")
                return
        await event.answer("Уже убрано")

    @bot.on(events.CallbackQuery(pattern=rb"(on|off)(\d+)"))
    async def cb_toggle_position(event):
        if event.sender_id not in ADMIN_IDS:
            await event.answer("Нет доступа")
            return
        action = event.pattern_match.group(1).decode()
        idx = int(event.pattern_match.group(2))
        data = load_data()
        if 0 <= idx < len(data["positions"]):
            data["positions"][idx]["on"] = (action == "on")
            save_data(data)
            lines = []
            for i, p in enumerate(data["positions"]):
                status = "▶️" if p.get("on", True) else "⏸"
                kw_str = ', '.join(p["kw"])
                price_str = f"  — {p['pr']}₽" if p.get("pr") else ""
                lines.append(f"{status} {i + 1}. {kw_str}{price_str}")
            buttons = []
            for i, p in enumerate(data["positions"]):
                row = []
                if p.get("on", True):
                    row.append(Button.inline(f"⏸ {i+1}", data=f"off{i}".encode()))
                else:
                    row.append(Button.inline(f"▶️ {i+1}", data=f"on{i}".encode()))
                ph = pos_hash(p["kw"])
                row.append(Button.inline(f"❌ {i+1}", data=f"rp{ph}".encode()))
                buttons.append(row)
            await event.edit(
                "📋 Позиции:\n" + "\n".join(lines),
                buttons=buttons,
            )
        else:
            await event.answer("Позиция не найдена")

    @bot.on(events.CallbackQuery(pattern=rb"stop(\w+)"))
    async def cb_stop_position(event):
        if event.sender_id not in ADMIN_IDS:
            await event.answer("Нет доступа")
            return
        ph = event.pattern_match.group(1).decode()
        data = load_data()
        for pos in data["positions"]:
            if pos_hash(pos["kw"]) == ph:
                pos["on"] = False
                save_data(data)
                await event.answer(f"⏸ {', '.join(pos['kw'])} выключена")
                return
        await event.answer("Позиция не найдена")

    # ── команда /app ─────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/app", from_users=list(ADMIN_IDS)))
    async def cmd_app(event):
        if not WEBAPP_URL:
            await event.respond("WEBAPP_URL не настроен в .env")
            return
        url = WEBAPP_URL.rstrip("/") + "/app/"
        await event.respond(
            "\U0001f4f1 Панель управления",
            buttons=[[Button.url("Открыть", url)]],
        )

    # ── мониторинг каналов (юзербот) ──────────────────────────────────────

    DEDUP_DELAY = 15
    _pending: dict = {}

    async def _flush_pending(key):
        await asyncio.sleep(DEDUP_DELAY)
        entry = _pending.pop(key, None)
        if not entry:
            return

        channels_info = entry["channels"]
        buyer_line = entry["buyer_line"]
        buyer_url = entry["buyer_url"]
        matched = entry["matched"]
        display = entry["display"]
        qty = entry["qty"]

        if len(channels_info) == 1:
            ch_title, msg_link, msg_time = channels_info[0]
            channels_text = (
                f"Канал: {ch_title}\n\n"
                f"Ссылка: {msg_link}  🕐 {msg_time}"
            )
        else:
            lines = []
            for ch_title, msg_link, msg_time in channels_info:
                lines.append(f"• {ch_title} 🕐 {msg_time}\n  {msg_link}")
            channels_text = (
                f"Каналы ({len(channels_info)}):\n" + "\n".join(lines)
            )

        copy_lines = []
        for m in matched:
            kw = m["kw"] if isinstance(m, dict) else m
            price = m.get("pr", "") if isinstance(m, dict) else ""
            parts = [", ".join(kw)]
            if price:
                parts.append(f"{price}₽")
            if qty:
                parts.append(f"{qty} шт")
            copy_lines.append(" — ".join(parts))
        pos_str = "```\n" + "\n".join(copy_lines) + "\n```"

        notification = (
            f"📢 Совпадение!\n\n"
            f"{channels_text}\n\n"
            f"Покупатель: {buyer_line}\n\n"
            f"Сообщение:\n{display}\n\n"
            f"Позиция:\n{pos_str}"
        )

        buttons = []
        if buyer_url:
            buttons.append([Button.url("💬 Чат", buyer_url)])
        for m in matched:
            if isinstance(m, dict):
                ph = pos_hash(m["kw"])
                buttons.append([Button.inline(
                    f"⏸ Стоп: {', '.join(m['kw'])[:30]}",
                    data=f"stop{ph}".encode(),
                )])

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

        for m in matched:
            if isinstance(m, dict):
                record_match(chat.id, pos_hash(m["kw"]))

        ch_title = getattr(chat, "title", str(chat.id))
        ch_username = getattr(chat, "username", None)
        if ch_username:
            msg_link = f"https://t.me/{ch_username}/{event.id}"
        else:
            msg_link = f"https://t.me/c/{chat.id}/{event.id}"

        sender = await event.get_sender()
        buyer_url = None
        sender_id = 0
        if isinstance(sender, User):
            sender_id = sender.id
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

        MSK = timezone(timedelta(hours=3))
        msg_time = event.date.astimezone(MSK).strftime("%H:%M")

        qty_match = re.search(
            r'(?:(\d+)\s*(?:шт|штук|pcs?|units?))|(?:[xхXХ](\d+))|(?:(\d+)\s*[xхXХ])',
            msg_text, re.I,
        )
        qty = None
        if qty_match:
            qty = qty_match.group(1) or qty_match.group(2) or qty_match.group(3)

        display = msg_text[:800] + ("..." if len(msg_text) > 800 else "")

        pos_key = tuple(sorted(
            pos_hash(m["kw"]) for m in matched if isinstance(m, dict)
        ))
        key = (sender_id, pos_key)

        if key in _pending:
            _pending[key]["channels"].append((ch_title, msg_link, msg_time))
            if len(msg_text) > len(_pending[key]["raw_text"]):
                _pending[key]["display"] = display
                _pending[key]["raw_text"] = msg_text
            log.info("Дедупликация: %s из %s добавлен к pending", buyer_line.split('\n')[0], ch_title)
        else:
            _pending[key] = {
                "channels": [(ch_title, msg_link, msg_time)],
                "buyer_line": buyer_line,
                "buyer_url": buyer_url,
                "matched": matched,
                "display": display,
                "qty": qty,
                "raw_text": msg_text,
            }
            asyncio.create_task(_flush_pending(key))

    # ── веб-сервер ─────────────────────────────────────────────────────────

    web_app = _setup_web_app()
    runner = aio_web.AppRunner(web_app)
    await runner.setup()
    site = aio_web.TCPSite(runner, '127.0.0.1', 8082)
    await site.start()
    log.info("Web-сервер запущен на порту 8082")

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
