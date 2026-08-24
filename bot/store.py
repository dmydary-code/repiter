"""Состояние бота: один JSON-файл, который коммитится обратно в репозиторий."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("REPITER_STATE", "data/state.json"))

DEFAULT_WINDOW = [10, 22]  # часы по локальному времени пользователя
DEFAULT_TZ = "Europe/Moscow"
DEFAULT_MAX_PER_DAY = 8


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def empty_state() -> dict:
    return {"version": 1, "offset": 0, "heartbeat": "", "users": {}}


def load() -> dict:
    if not STATE_PATH.exists():
        return empty_state()
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[store] не удалось прочитать состояние ({e}), начинаю с пустого")
        return empty_state()
    base = empty_state()
    base.update(data)
    for key in list(base["users"]):
        get_user(base, int(key))  # доставит недостающие поля и мигрирует карточки
    return base


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(STATE_PATH)


def get_user(state: dict, chat_id: int) -> dict:
    key = str(chat_id)
    user = state["users"].get(key)
    if user is None:
        user = {
            "chat_id": chat_id,
            "lang": None,
            "stage": "awaiting_lang",
            "tz": DEFAULT_TZ,
            "window": list(DEFAULT_WINDOW),
            "max_per_day": DEFAULT_MAX_PER_DAY,
            "paused": False,
            "created_at": iso(now_utc()),
            "sent_date": "",
            "sent_today": 0,
            "cards": [],
        }
        state["users"][key] = user
    # миграция недостающих полей
    user.setdefault("max_per_day", DEFAULT_MAX_PER_DAY)
    user.setdefault("paused", False)
    user.setdefault("sent_date", "")
    user.setdefault("sent_today", 0)
    user.setdefault("window", list(DEFAULT_WINDOW))
    user.setdefault("tz", DEFAULT_TZ)
    user.setdefault("last_deliver_tick", None)
    user.pop("last_reminder_at", None)
    for card in user["cards"]:
        migrate_card(card)
    return user


def new_card(word: str, lang: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "word": word,
        "lang": lang,
        "word_ru": "",
        "step": 0,
        "reps": 0,
        "lapses": 0,
        "archived": False,
        "due_tick": None,  # проставит srs.schedule
        "last_sent_at": None,
        "created_at": iso(now_utc()),
        "cache": [],
    }


def migrate_card(card: dict) -> dict:
    """Карточки из версии, где время считалось часами, а не тиками."""
    if "due_tick" not in card:
        moment = parse(card.pop("send_at", None) or card.pop("due_at", None))
        card["due_tick"] = (
            None if moment is None else int(moment.timestamp()) // (30 * 60)
        )
    card.pop("send_at", None)
    card.pop("due_at", None)
    card.setdefault("cache", [])
    card.setdefault("word_ru", "")
    return card


def find_card(user: dict, card_id: str) -> dict | None:
    for card in user["cards"]:
        if card["id"] == card_id:
            return card
    return None


def active_cards(user: dict) -> list[dict]:
    return [c for c in user["cards"] if not c["archived"]]
