"""Один «тик» бота: разобрать входящие апдейты и разослать напоминания.

Запускается по расписанию из GitHub Actions. Постоянного процесса нет —
всё состояние живёт в data/state.json и коммитится обратно в репозиторий.
"""

from __future__ import annotations

import os
import random
import sys
from zoneinfo import ZoneInfo

from . import render, srs, store
from .grok import GrokUnavailable, generate
from .store import iso, now_utc
from .tg import Telegram, esc

# За один тик уходит не больше одного напоминания: полчаса между словами —
# и нет риска получить пачку разом.
MAX_SENDS_PER_TICK = 1

COMMANDS = [
    {"command": "list", "description": "что сейчас учу"},
    {"command": "stats", "description": "статистика"},
    {"command": "lang", "description": "сменить язык"},
    {"command": "window", "description": "часы напоминаний, например /window 10 22"},
    {"command": "limit", "description": "сколько напоминаний в день"},
    {"command": "delete", "description": "убрать слово"},
    {"command": "pause", "description": "поставить на паузу"},
    {"command": "resume", "description": "снять с паузы"},
    {"command": "help", "description": "справка"},
]


# --------------------------------------------------------------------------
# Входящие сообщения
# --------------------------------------------------------------------------

def add_words(tg: Telegram, user: dict, raw: str) -> None:
    chunks = [
        part.strip(" \t.;")
        for line in raw.splitlines()
        for part in line.split(",")
    ]
    words = [w for w in chunks if w]
    if not words:
        return
    words = words[:10]  # разумный потолок на одно сообщение

    existing = {c["word"].lower() for c in user["cards"] if not c["archived"]}
    added, skipped = [], []

    for word in words:
        if word.lower() in existing:
            skipped.append(word)
            continue
        card = store.new_card(word, user["lang"])
        # Просим Grok сразу три примера: один пойдёт в подтверждение,
        # остальные лягут в кеш на случай, если API однажды не ответит.
        try:
            data = generate(word, user["lang"], count=3)
            card["word_ru"] = data["word_ru"]
            card["cache"] = data["examples"]
        except GrokUnavailable as e:
            print(f"[api] {word}: {e}")
        srs.schedule(card, srs.FIRST_SHOW)
        user["cards"].append(card)
        existing.add(word.lower())
        added.append(card)

    parts = []
    if added:
        rows = []
        for card in added:
            translation = card.get("word_ru")
            hint = (
                f" — <tg-spoiler>{esc(translation)}</tg-spoiler>" if translation else ""
            )
            rows.append(f"• <b>{esc(card['word'])}</b>{hint}")
        plural = "Добавила" if len(added) == 1 else f"Добавила {len(added)}"
        parts.append(f"✅ {plural}:\n" + "\n".join(rows))
        parts.append("Подкину, когда не ждёшь 👀")
    if skipped:
        parts.append("Уже в колоде: " + ", ".join(f"<b>{esc(w)}</b>" for w in skipped))

    tg.send_message(user["chat_id"], "\n\n".join(parts))


def handle_command(tg: Telegram, state: dict, user: dict, text: str) -> None:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    chat_id = user["chat_id"]

    if cmd == "/start":
        if user["lang"]:
            tg.send_message(chat_id, render.help_text(user))
        else:
            tg.send_message(chat_id, render.welcome(), render.lang_keyboard())
        return

    if cmd == "/help":
        tg.send_message(chat_id, render.help_text(user))
        return

    if cmd == "/lang":
        tg.send_message(chat_id, "Какой язык учим?", render.lang_keyboard())
        return

    if cmd == "/list":
        tg.send_message(chat_id, render.card_list(user, store.active_cards(user)))
        return

    if cmd == "/stats":
        tg.send_message(chat_id, render.stats(user))
        return

    if cmd == "/pause":
        user["paused"] = True
        tg.send_message(chat_id, "Замолкаю 🤐 Вернуть меня — /resume")
        return

    if cmd == "/resume":
        user["paused"] = False
        tg.send_message(chat_id, "Снова в деле 🦜")
        return

    if cmd == "/window":
        try:
            start, end = int(args[0]), int(args[1])
            if not (0 <= start < end <= 24):
                raise ValueError
        except (IndexError, ValueError):
            tg.send_message(chat_id, "Формат: <code>/window 10 22</code>")
            return
        user["window"] = [start, end]
        tg.send_message(chat_id, f"Пишу только с {start}:00 до {end}:00 ⏰")
        return

    if cmd == "/limit":
        try:
            limit = int(args[0])
            if not 1 <= limit <= 30:
                raise ValueError
        except (IndexError, ValueError):
            tg.send_message(chat_id, "Формат: <code>/limit 8</code> (от 1 до 30)")
            return
        user["max_per_day"] = limit
        tg.send_message(chat_id, f"Не больше {limit} напоминаний в день 👌")
        return

    if cmd == "/delete":
        target = " ".join(args).strip().lower()
        if not target:
            tg.send_message(chat_id, "Формат: <code>/delete serendipity</code>")
            return
        before = len(user["cards"])
        user["cards"] = [c for c in user["cards"] if c["word"].lower() != target]
        if len(user["cards"]) < before:
            tg.send_message(chat_id, f"Убрала <b>{esc(target)}</b> 🗑")
        else:
            tg.send_message(chat_id, "Такого слова у меня нет.")
        return

    tg.send_message(chat_id, "Не знаю такой команды. /help")


def handle_message(tg: Telegram, state: dict, msg: dict) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return

    user = store.get_user(state, chat_id)

    if text.startswith("/"):
        handle_command(tg, state, user, text)
        return

    if not user["lang"]:
        tg.send_message(chat_id, render.welcome(), render.lang_keyboard())
        return

    add_words(tg, user, text)


def handle_callback(tg: Telegram, state: dict, cb: dict) -> None:
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if chat_id is None:
        return

    user = store.get_user(state, chat_id)

    if data == "noop":
        tg.answer_callback(cb["id"])
        return

    if data.startswith("lang:"):
        lang = data.split(":", 1)[1]
        if lang not in ("en", "fr"):
            return
        user["lang"] = lang
        user["stage"] = "ready"
        tg.answer_callback(cb["id"], "Готово")
        if message_id:
            tg.edit_reply_markup(chat_id, message_id, [])
        tg.send_message(chat_id, render.language_set(lang))
        return

    if ":" not in data:
        return
    action, card_id = data.split(":", 1)
    card = store.find_card(user, card_id)
    if card is None:
        tg.answer_callback(cb["id"], "Это слово я уже не помню 🙈")
        return

    if action == "ok":
        srs.on_known(card)
    elif action == "later":
        srs.on_later(card)
    else:
        return

    card["pending_msg"] = None
    tg.answer_callback(cb["id"], "Записала")
    if message_id:
        tg.edit_reply_markup(
            chat_id,
            message_id,
            render.answered_keyboard(action == "ok", card["archived"]),
        )


def process_updates(tg: Telegram, state: dict, poll: int = 0) -> int:
    updates = tg.get_updates(state.get("offset", 0), poll=poll)
    for update in updates:
        state["offset"] = update["update_id"] + 1
        try:
            if "message" in update:
                handle_message(tg, state, update["message"])
            elif "callback_query" in update:
                handle_callback(tg, state, update["callback_query"])
        except Exception as e:  # noqa: BLE001 — один кривой апдейт не должен ронять тик
            print(f"[update {update['update_id']}] ошибка: {e}", file=sys.stderr)
    return len(updates)


# --------------------------------------------------------------------------
# Исходящие напоминания
# --------------------------------------------------------------------------

def pick_example(card: dict) -> dict | None:
    """Свежий пример от Grok, а если не вышло — из кеша."""
    try:
        data = generate(card["word"], card["lang"], count=1)
        if not card.get("word_ru") and data.get("word_ru"):
            card["word_ru"] = data["word_ru"]
        example = data["examples"][0]
        # Подкладываем в кеш про запас, храним не больше пяти.
        card["cache"] = ([example] + card.get("cache", []))[:5]
        return example
    except GrokUnavailable as e:
        print(f"[grok] {card['word']}: {e}")

    cache = card.get("cache") or []
    if cache:
        return random.choice(cache)
    return None


def send_reminder(tg: Telegram, user: dict, card: dict, tick: int) -> bool:
    example = pick_example(card)
    if example is None:
        # Напоминание без живого предложения бессмысленно: одно голое слово
        # с переводом ничего не закрепляет. Молчим — карточка остаётся
        # назревшей и уйдёт следующим тиком.
        print(f"[send] «{card['word']}»: примера нет, жду следующего тика")
        return False

    res = tg.send_message(
        user["chat_id"],
        render.reminder(card, example),
        render.reminder_keyboard(card["id"]),
    )
    if not res.get("ok"):
        print(f"[tg] не отправилось «{card['word']}»: {res.get('description')}")
        return False

    card["last_sent_at"] = iso(now_utc())
    card["pending_msg"] = (res.get("result") or {}).get("message_id")
    # Кнопку могут не нажать — тогда переспросим через сутки.
    srs.schedule(card, srs.NO_ANSWER, tick)
    return True


def deliver(tg: Telegram, user: dict, tick: int | None = None) -> int:
    """Один тик рассылки. Вызов повторно с тем же номером тика ничего
    не делает — так перезапуск процесса не приводит к дублям."""
    if user.get("paused") or not user.get("lang"):
        return 0

    tick = srs.tick_now() if tick is None else tick
    moment = now_utc()
    tz = ZoneInfo(user.get("tz", "Europe/Moscow"))
    today = moment.astimezone(tz).date().isoformat()
    if user.get("sent_date") != today:
        user["sent_date"] = today
        user["sent_today"] = 0

    # Ночью тики идут, но бот молчит: назревшее подождёт до утра.
    if not srs.in_window(user, moment):
        return 0

    if user.get("last_deliver_tick") == tick:
        return 0
    user["last_deliver_tick"] = tick

    budget = min(MAX_SENDS_PER_TICK, user.get("max_per_day", 8) - user["sent_today"])
    if budget <= 0:
        return 0

    due = [c for c in store.active_cards(user) if srs.is_due(c, tick)]
    due.sort(key=lambda c: c["due_tick"])  # что назрело раньше, то и первое

    sent = 0
    for card in due[:budget]:
        if send_reminder(tg, user, card, tick):
            sent += 1
            user["sent_today"] += 1
    return sent


# --------------------------------------------------------------------------

def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан", file=sys.stderr)
        return 1

    tg = Telegram(token)
    state = store.load()

    updates = process_updates(tg, state)

    sent = 0
    for user in state["users"].values():
        try:
            sent += deliver(tg, user)
        except Exception as e:  # noqa: BLE001
            print(f"[deliver {user.get('chat_id')}] ошибка: {e}", file=sys.stderr)

    if not state.get("commands_set"):
        if tg.set_my_commands(COMMANDS).get("ok"):
            state["commands_set"] = True

    # Меняется раз в сутки — гарантирует хотя бы один коммит в день,
    # чтобы GitHub не отключил расписание за неактивность репозитория.
    state["heartbeat"] = now_utc().date().isoformat()

    store.save(state)
    print(f"тик: апдейтов {updates}, напоминаний {sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
