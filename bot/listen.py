"""Живой цикл бота: длинный опрос Telegram + рассылка напоминаний.

На команды и присланные слова бот отвечает сразу — соединение с Telegram
держится открытым (long polling), апдейт прилетает в ту же секунду, как ты
нажала «отправить». Напоминания живут по своему расписанию: раз в минуту
проверяется, не назрела ли какая-нибудь карточка.

Job в GitHub Actions живёт максимум 6 часов, поэтому цикл сам аккуратно
завершается чуть раньше, а расписание в listen.yml поднимает следующий.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from . import gitsync, store
from .main import COMMANDS, deliver, process_updates
from .store import now_utc
from .tg import Telegram

# Сколько секунд Telegram держит соединение, если сообщений нет.
POLL_TIMEOUT = 25

# Как часто проверять, не пора ли отправить напоминание.
DELIVER_EVERY = 60

# Не чаще этого коммитим состояние в репозиторий. Держим маленьким: job могут
# отменить в любой момент, и всё, что не доехало до репозитория, потеряется.
FLUSH_EVERY = 5

# Сколько живёт один запуск. Лимит job-а в Actions — 6 часов, уходим раньше,
# чтобы успеть спокойно дописать состояние.
DEFAULT_RUNTIME = 5 * 3600 + 40 * 60

_stop = False


def _request_stop(signum, _frame):
    global _stop
    print(f"[listen] получен сигнал {signum}, закругляюсь")
    _stop = True


def run(tg: Telegram | None = None, runtime: float | None = None) -> int:
    global _stop
    _stop = False

    if tg is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            print("TELEGRAM_BOT_TOKEN не задан", file=sys.stderr)
            return 1
        tg = Telegram(token)

    if runtime is None:
        runtime = float(os.environ.get("REPITER_RUNTIME", DEFAULT_RUNTIME))

    state = store.load()
    if not state.get("commands_set") and tg.set_my_commands(COMMANDS).get("ok"):
        state["commands_set"] = True

    started = time.monotonic()
    deadline = started + runtime
    last_deliver = 0.0
    last_flush = time.monotonic()
    dirty = True  # первый проход всегда сохраняем: могли обновиться команды
    updates_total = sent_total = 0

    print(f"[listen] старт, работаю {runtime / 3600:.1f} ч")

    while not _stop and time.monotonic() < deadline:
        # 1. Входящее — отвечаем мгновенно.
        try:
            n = process_updates(tg, state, poll=POLL_TIMEOUT)
            if n:
                updates_total += n
                dirty = True
        except Exception as e:  # noqa: BLE001
            print(f"[listen] опрос упал: {e}", file=sys.stderr)
            time.sleep(5)

        # 2. Исходящее — по своему расписанию.
        if time.monotonic() - last_deliver >= DELIVER_EVERY:
            last_deliver = time.monotonic()
            for user in state["users"].values():
                try:
                    sent = deliver(tg, user)
                except Exception as e:  # noqa: BLE001
                    print(f"[listen] рассылка {user.get('chat_id')}: {e}", file=sys.stderr)
                    continue
                if sent:
                    sent_total += sent
                    dirty = True

        # 3. Отметка живости — меняется раз в сутки, чтобы GitHub не отключил
        #    расписание за неактивность репозитория.
        today = now_utc().date().isoformat()
        if state.get("heartbeat") != today:
            state["heartbeat"] = today
            dirty = True

        # 4. Сохранение.
        if dirty and time.monotonic() - last_flush >= FLUSH_EVERY:
            store.save(state)
            gitsync.push_state(f"state: {now_utc():%Y-%m-%dT%H:%MZ}")
            dirty = False
            last_flush = time.monotonic()

    store.save(state)
    gitsync.push_state(f"state: {now_utc():%Y-%m-%dT%H:%MZ}")
    print(
        f"[listen] финиш: прожил {(time.monotonic() - started) / 60:.0f} мин, "
        f"апдейтов {updates_total}, напоминаний {sent_total}"
    )
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
