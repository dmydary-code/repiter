"""Прогон полного сценария на подменённых Telegram и Grok.

Запуск:  python -m tests.test_flow
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TMP = Path(tempfile.mkdtemp())
os.environ["REPITER_STATE"] = str(TMP / "state.json")
os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"

from bot import listen, main, render, srs, store  # noqa: E402
from bot.grok import GrokUnavailable  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
CHAT = 4242


# --- заглушки --------------------------------------------------------------

class FakeTelegram:
    def __init__(self):
        self.inbox: list[dict] = []
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._msg_id = 1000
        self._update_id = 500

    # то, что "приходит" от пользователя
    def push_text(self, text: str):
        self._update_id += 1
        self.inbox.append({
            "update_id": self._update_id,
            "message": {"chat": {"id": CHAT}, "text": text},
        })

    def push_callback(self, data: str, message_id: int):
        self._update_id += 1
        self.inbox.append({
            "update_id": self._update_id,
            "callback_query": {
                "id": f"cb{self._update_id}",
                "data": data,
                "message": {"chat": {"id": CHAT}, "message_id": message_id},
            },
        })

    # интерфейс, который использует бот
    def get_updates(self, offset, limit=100, poll=0):
        ready = [u for u in self.inbox if u["update_id"] >= offset]
        self.inbox = []
        return ready

    def send_message(self, chat_id, text, keyboard=None, preview=False):
        self._msg_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard,
                          "message_id": self._msg_id})
        return {"ok": True, "result": {"message_id": self._msg_id}}

    def edit_reply_markup(self, chat_id, message_id, keyboard=None):
        self.edits.append({"message_id": message_id, "keyboard": keyboard})
        return {"ok": True}

    def edit_message(self, *a, **k):
        return {"ok": True}

    def answer_callback(self, callback_id, text=""):
        return {"ok": True}

    def set_my_commands(self, commands):
        return {"ok": True}

    def last(self):
        return self.sent[-1]["text"] if self.sent else ""


def fake_generate(word, lang, count=1, api_key=None):
    return {
        "word_ru": f"перевод-{word}",
        "examples": [
            {
                "sentence": f"Nobody expected the {word}, and yet here we are.",
                "word_form": word,
                "sentence_ru": f"Никто не ждал {word}, но вот.",
            }
            for _ in range(count)
        ],
    }


def failing_generate(word, lang, count=1, api_key=None):
    raise GrokUnavailable("тестовый сбой")


# --- помощники -------------------------------------------------------------

CHECKS = {"ok": 0, "fail": 0}


def check(label: str, condition: bool, extra: str = ""):
    if condition:
        CHECKS["ok"] += 1
        print(f"  ✓ {label}")
    else:
        CHECKS["fail"] += 1
        print(f"  ✗ {label} {extra}")


def tick(tg, at: datetime | None = None):
    """Один запуск бота; at — подменённое «сейчас» в UTC."""
    real_now = store.now_utc
    if at is not None:
        for module in (store, srs, main):
            module.now_utc = lambda _at=at: _at
    try:
        state = store.load()
        main.process_updates(tg, state)
        for user in state["users"].values():
            main.deliver(tg, user)
        store.save(state)
        return state
    finally:
        for module in (store, srs, main):
            module.now_utc = real_now


def user_of(state):
    return state["users"][str(CHAT)]


def day_at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    base = datetime.now(MSK).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).astimezone(timezone.utc)


# --- сценарий --------------------------------------------------------------

def run():
    tg = FakeTelegram()
    main.generate = fake_generate

    print("\n1. /start и выбор языка")
    tg.push_text("/start")
    tick(tg, day_at(0, 11))
    check("бот поздоровался", "Репитер" in tg.last())
    check("предложил выбор языка", tg.sent[-1]["keyboard"] == render.lang_keyboard())

    lang_msg = tg.sent[-1]["message_id"]
    tg.push_callback("lang:en", lang_msg)
    state = tick(tg, day_at(0, 11, 1))
    check("язык сохранён", user_of(state)["lang"] == "en")
    check("прислал инструкцию", "английский" in tg.last())

    print("\n2. Добавление слов")
    tg.push_text("serendipity\nquagmire, brouhaha")
    state = tick(tg, day_at(0, 11, 5))
    cards = store.active_cards(user_of(state))
    check("три карточки", len(cards) == 3, f"(получено {len(cards)})")
    check("перевод подтянулся", all(c["word_ru"] for c in cards))
    check("кеш примеров заполнен", all(len(c["cache"]) == 3 for c in cards))
    check("в подтверждении есть спойлер", "<tg-spoiler>" in tg.last())
    check("первый показ через ~25 мин", all(c["step"] == 0 for c in cards))

    print("\n3. Дубликаты не плодятся")
    tg.push_text("serendipity")
    state = tick(tg, day_at(0, 11, 10))
    check("осталось три карточки", len(store.active_cards(user_of(state))) == 3)
    check("бот сказал, что слово уже есть", "Уже в колоде" in tg.last())

    print("\n4. Напоминание приходит в окне")
    before = len(tg.sent)
    state = tick(tg, day_at(0, 17))
    fresh = tg.sent[before:]
    check("за один проход уходит ровно одно", len(fresh) == main.MAX_SENDS_PER_CYCLE)
    check("что-то пришло", len(fresh) >= 1, f"(пришло {len(fresh)})")
    reminder = fresh[0]
    check("слово выделено жирным", "<b>" in reminder["text"])
    check("перевод спрятан", "<tg-spoiler>" in reminder["text"])
    check("две кнопки", len(reminder["keyboard"][0]) == 2)
    check("кнопка «Я выучила»", reminder["keyboard"][0][0]["text"] == "✅ Я выучила")

    print("\n5. Ночью бот молчит")
    before = len(tg.sent)
    tick(tg, day_at(1, 3))
    check("в 3 ночи ни одного сообщения", len(tg.sent) == before)

    print("\n6. Кнопки двигают карточку по лесенке")
    card_id = reminder["keyboard"][0][0]["callback_data"].split(":", 1)[1]
    tg.push_callback(f"ok:{card_id}", reminder["message_id"])
    state = tick(tg, day_at(0, 17, 5))
    card = store.find_card(user_of(state), card_id)
    check("шаг вырос до 1", card["step"] == 1, f"(шаг {card['step']})")
    check("клавиатура заменена на итог", "Выучено" in tg.edits[-1]["keyboard"][0][0]["text"])
    next_at = store.parse(card["send_at"]).astimezone(MSK)
    check("следующий показ внутри окна 10–22", 10 <= next_at.hour < 22,
          f"(в {next_at:%d.%m %H:%M})")

    tg.push_callback(f"later:{card_id}", reminder["message_id"])
    state = tick(tg, day_at(0, 17, 10))
    card = store.find_card(user_of(state), card_id)
    check("«ещё хочу» вернул на шаг назад", card["step"] == 0, f"(шаг {card['step']})")
    check("засчитан возврат", card["lapses"] == 1)

    print("\n7. Слово доходит до архива")
    state = store.load()
    user = user_of(state)
    card = store.find_card(user, card_id)
    labels = []
    for _ in range(len(srs.STEPS)):
        labels.append(srs.on_known(card, user))
    store.save(state)
    check("карточка в архиве", card["archived"] is True)
    check("последний ярлык — архив", labels[-1] == "архив")
    check("лесенка из 8 шагов", len(srs.STEPS) == 8)
    check("интервалы растут",
          all(srs.STEPS[i] < srs.STEPS[i + 1] for i in range(len(srs.STEPS) - 1)))

    print("\n8. Grok упал — бот работает дальше")
    main.generate = failing_generate
    tg.push_text("kerfuffle")
    state = tick(tg, day_at(0, 12))
    kerfuffle = next(c for c in user_of(state)["cards"] if c["word"] == "kerfuffle")
    check("карточка всё равно создана", kerfuffle is not None)
    check("кеш пуст, но это не помеха", kerfuffle["cache"] == [])

    # делаем показ гарантированно назревшим
    state = store.load()
    user = user_of(state)
    for c in store.active_cards(user):
        c["send_at"] = store.iso(day_at(0, 12, 45)) if c["word"] == "kerfuffle" else None
    user["sent_today"] = 0
    user["last_reminder_at"] = None
    store.save(state)
    before = len(tg.sent)
    tick(tg, day_at(0, 13))
    fresh = tg.sent[before:]
    check("напоминание всё равно ушло", len(fresh) == 1, f"(ушло {len(fresh)})")
    check("без примера, но с переводом-заглушкой",
          "kerfuffle" in fresh[0]["text"] and "Grok был занят" in fresh[0]["text"])
    main.generate = fake_generate

    print("\n9. Дневной лимит")
    state = store.load()
    user = user_of(state)
    user["max_per_day"] = 2
    user["sent_today"] = 2
    user["sent_date"] = datetime.now(MSK).date().isoformat()
    for c in store.active_cards(user):
        c["send_at"] = store.iso(day_at(0, 12))
    store.save(state)
    before = len(tg.sent)
    state = tick(tg, day_at(0, 14))
    check("сверх лимита не шлём", len(tg.sent) == before)
    moved = [store.parse(c["send_at"]) for c in store.active_cards(user_of(state))]
    check("отложенное перенесено вперёд", all(m > day_at(0, 14) for m in moved))

    print("\n10. Команды")
    state = store.load()
    user_of(state)["max_per_day"] = 8
    store.save(state)
    for cmd, expect in [
        ("/list", "В работе"),
        ("/stats", "Статистика"),
        ("/window 9 21", "9:00"),
        ("/limit 5", "5 напоминаний"),
        ("/pause", "Замолкаю"),
        ("/resume", "Снова в деле"),
        ("/delete kerfuffle", "Убрала"),
        ("/help", "Что я умею"),
        ("/nonsense", "Не знаю такой"),
    ]:
        tg.push_text(cmd)
        before = len(tg.sent)
        tick(tg, day_at(0, 12))
        replies = " ".join(m["text"] for m in tg.sent[before:])
        check(f"{cmd} отвечает", expect in replies, f"→ {replies[:70]!r}")

    state = store.load()
    check("окно применилось", user_of(state)["window"] == [9, 21])
    check("kerfuffle удалён",
          all(c["word"] != "kerfuffle" for c in user_of(state)["cards"]))

    print("\n11. Экранирование и жирный текст")
    html = render.bold_word("The <script> was pure serendipity & chaos",
                            ["serendipity", "serendipity"])
    check("угловые скобки экранированы", "&lt;script&gt;" in html)
    check("амперсанд экранирован", "&amp;" in html)
    check("слово выделено", "<b>serendipity</b>" in html)
    html2 = render.bold_word("She was quagmired again", ["quagmire"])
    check("нашлась изменённая форма", "<b>quagmired</b>" in html2, f"→ {html2!r}")

    print("\n12. Рандомизация показа")
    user = {"tz": "Europe/Moscow", "window": [10, 22]}
    picks = {srs.pick_send_time(day_at(0, 11), user).astimezone(MSK).strftime("%H:%M")
             for _ in range(30)}
    check("время показа действительно случайное", len(picks) > 5, f"(вариантов {len(picks)})")
    night = srs.pick_send_time(day_at(0, 3), user).astimezone(MSK)
    check("ночное «пора» перенесено в окно", 10 <= night.hour < 22, f"(в {night:%H:%M})")
    late = srs.pick_send_time(day_at(0, 23), user).astimezone(MSK)
    check("позднее «пора» ушло на завтра", 10 <= late.hour < 22, f"(в {late:%H:%M})")

    print("\n13. Пауза между напоминаниями")
    state = store.load()
    user = user_of(state)
    user["max_per_day"] = 8
    user["sent_today"] = 0
    user["last_reminder_at"] = store.iso(day_at(0, 15, 58))
    for c in store.active_cards(user):
        c["send_at"] = store.iso(day_at(0, 15))
    store.save(state)
    before = len(tg.sent)
    tick(tg, day_at(0, 16))
    check("через 2 минуты после прошлого — молчим", len(tg.sent) == before)
    tick(tg, day_at(0, 16, 5))
    check("через 7 минут — можно", len(tg.sent) == before + 1)

    print("\n14. Живой цикл отвечает сразу")
    tg.push_text("/stats")
    before = len(tg.sent)
    code = listen.run(tg, runtime=0.4)
    check("цикл завершился штатно", code == 0)
    replies = " ".join(m["text"] for m in tg.sent[before:])
    check("ответ пришёл в том же цикле", "Статистика" in replies)
    check("длинный опрос включён", listen.POLL_TIMEOUT > 0)
    check("рассылка проверяется раз в минуту", listen.DELIVER_EVERY == 60)
    check("смена короче лимита job-а в 6 часов", listen.DEFAULT_RUNTIME < 6 * 3600)

    print(f"\n{'─' * 46}")
    print(f"Пройдено: {CHECKS['ok']}   Провалено: {CHECKS['fail']}")
    return 0 if CHECKS["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
