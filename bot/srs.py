"""Интервальные повторения, измеренные в тиках.

Тик — полчаса. Время внутри бота считается не часами и минутами, а номером
тика: `tick = unix_seconds // 1800`. Номер глобальный и не зависит от того,
когда процесс запустился, поэтому перезапуск смены в Actions ничего не сдвигает.

Лесенка (в тиках от момента последнего показа):

    первый показ    +2      ближайший тик пропускаем, показ на следующем
    шаг 1           +16     8 часов
    шаг 2           +48     сутки
    шаг 3          +144     3 дня
    шаг 4          +336     неделя
    шаг 5          +672     2 недели
    шаг 6         +1440     месяц
    шаг 7         +4320     3 месяца → архив

Все значения — ровно 48 тиков на сутки. Чтобы поменять шаг лесенки,
достаточно поправить одно число в LADDER.

Кнопки дают бинарный сигнал: «Я выучила» — шаг вперёд, «Ещё хочу потом» —
шаг назад и скорый повтор.
"""

from __future__ import annotations

import time
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

TICK_SECONDS = 30 * 60

# Сколько тиков ждать перед каждым следующим показом.
LADDER = [2, 16, 48, 144, 336, 672, 1440, 4320]

# Свежее слово показываем не в ближайший тик, а через один.
FIRST_SHOW = LADDER[0]

# Если на сообщение не нажали ни одну кнопку — переспросим через сутки.
NO_ANSWER = 48


def tick_now(moment: datetime | None = None) -> int:
    """Номер текущего тика."""
    seconds = moment.timestamp() if moment is not None else time.time()
    return int(seconds) // TICK_SECONDS


def tick_of(moment: datetime) -> int:
    return int(moment.timestamp()) // TICK_SECONDS


def step_label(step: int) -> str:
    """Описание шага — только для логов, пользователю мы это не показываем."""
    if step >= len(LADDER):
        return "архив"
    return f"шаг {step + 1}, +{LADDER[step]} тиков"


def schedule(card: dict, ticks: int, now: int | None = None) -> None:
    card["due_tick"] = (tick_now() if now is None else now) + ticks


def on_known(card: dict, now: int | None = None) -> str:
    """Нажата «Я выучила»."""
    card["reps"] += 1
    card["step"] += 1
    if card["step"] >= len(LADDER):
        card["archived"] = True
        card["due_tick"] = None
        return "архив"
    schedule(card, LADDER[card["step"]], now)
    return step_label(card["step"])


def on_later(card: dict, now: int | None = None) -> str:
    """Нажата «Ещё хочу потом» — слово ещё не осело."""
    card["lapses"] += 1
    card["step"] = max(0, card["step"] - 1)
    schedule(card, LADDER[card["step"]], now)
    return step_label(card["step"])


def is_due(card: dict, tick: int) -> bool:
    return (
        not card["archived"]
        and card.get("due_tick") is not None
        and card["due_tick"] <= tick
    )


def in_window(user: dict, moment: datetime) -> bool:
    """Тики идут круглые сутки, но ночью бот молчит: назревшие карточки
    просто ждут первого тика внутри дневного окна."""
    tz = ZoneInfo(user.get("tz", "Europe/Moscow"))
    start_h, end_h = user.get("window", [10, 22])
    local = moment.astimezone(tz)
    start = datetime.combine(local.date(), clock(hour=start_h), tzinfo=tz)
    end = datetime.combine(local.date(), clock(hour=end_h), tzinfo=tz)
    if end <= start:
        end = start + timedelta(hours=1)
    return start <= local < end
