"""Интервальные повторения.

Лесенка интервалов основана на классической схеме SM-2 / «1-3-7-14-30»
с двумя дополнительными короткими шагами в первый день: первые часы после
знакомства со словом — самая крутая часть кривой забывания.

Кнопки в сообщении дают бинарный сигнал:
  «Я выучила»      → шаг вперёд по лесенке;
  «Ещё хочу потом» → шаг назад (минимум до нуля) и скорый повтор.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import iso, now_utc, parse

# Лесенка интервалов. Пройдя последний шаг, карточка уходит в архив.
STEPS = [
    timedelta(minutes=25),
    timedelta(hours=4),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=14),
    timedelta(days=30),
    timedelta(days=90),
]

# Насколько далеко от момента «пора» можно сдвинуть показ ради рандома.
MAX_JITTER = timedelta(hours=5)


def step_label(step: int) -> str:
    """Человеческое описание следующего интервала."""
    if step >= len(STEPS):
        return "слово выучено"
    delta = STEPS[step]
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"через {minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"через {hours} ч"
    days = hours // 24
    if days == 1:
        return "завтра"
    if days < 5:
        return f"через {days} дня"
    if days < 30:
        return f"через {days} дней"
    if days == 30:
        return "через месяц"
    return f"через {days // 30} мес"


def _window_bounds(day, tz: ZoneInfo, window: list[int]) -> tuple[datetime, datetime]:
    start_h, end_h = window
    start = datetime.combine(day, time(hour=start_h), tzinfo=tz)
    end = datetime.combine(day, time(hour=end_h), tzinfo=tz)
    if end <= start:  # защита от кривых настроек
        end = start + timedelta(hours=1)
    return start, end


def pick_send_time(due_at: datetime, user: dict, rng: random.Random | None = None) -> datetime:
    """Выбирает случайный момент показа внутри дневного окна пользователя.

    Если «пора» выпало на ночь — переносим на ближайшее утро.
    Если внутри окна — сдвигаем на случайную величину вперёд, но не дальше
    MAX_JITTER и не за пределы окна.
    """
    rng = rng or random
    tz = ZoneInfo(user.get("tz", "Europe/Moscow"))
    window = user.get("window", [10, 22])
    local = due_at.astimezone(tz)

    start, end = _window_bounds(local.date(), tz, window)
    if local < start:
        low, high = start, end
    elif local >= end:
        start, end = _window_bounds(local.date() + timedelta(days=1), tz, window)
        low, high = start, end
    else:
        low, high = local, end

    span = min((high - low).total_seconds(), MAX_JITTER.total_seconds())
    if span <= 0:
        chosen = low
    else:
        chosen = low + timedelta(seconds=rng.uniform(0, span))
    return chosen.astimezone(timezone.utc)


def schedule(card: dict, user: dict, delta: timedelta, rng: random.Random | None = None) -> None:
    """Проставляет карточке due_at и рандомизированный send_at."""
    due = now_utc() + delta
    card["due_at"] = iso(due)
    card["send_at"] = iso(pick_send_time(due, user, rng))


def on_known(card: dict, user: dict, rng: random.Random | None = None) -> str:
    """Нажата «Я выучила»."""
    card["reps"] += 1
    card["step"] += 1
    if card["step"] >= len(STEPS):
        card["archived"] = True
        card["send_at"] = None
        card["due_at"] = None
        return "архив"
    schedule(card, user, STEPS[card["step"]], rng)
    return step_label(card["step"])


def on_later(card: dict, user: dict, rng: random.Random | None = None) -> str:
    """Нажата «Ещё хочу потом» — слово ещё не осело."""
    card["lapses"] += 1
    card["step"] = max(0, card["step"] - 1)
    schedule(card, user, STEPS[card["step"]], rng)
    return step_label(card["step"])


def is_due_to_send(card: dict, moment: datetime) -> bool:
    if card["archived"] or not card.get("send_at"):
        return False
    send_at = parse(card["send_at"])
    return send_at is not None and send_at <= moment


def in_window(user: dict, moment: datetime) -> bool:
    tz = ZoneInfo(user.get("tz", "Europe/Moscow"))
    window = user.get("window", [10, 22])
    local = moment.astimezone(tz)
    start, end = _window_bounds(local.date(), tz, window)
    return start <= local < end
