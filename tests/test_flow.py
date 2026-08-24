"""Прогон полного сценария на подменённых Telegram и генераторе примеров.

Время внутри бота измеряется тиками по 30 минут, поэтому и тест двигает
не «часы», а номера тиков.

Запуск:  python -m tests.test_flow
"""

from __future__ import annotations

import os
import re
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
HOUR = 2  # тиков в часе


# --- заглушки --------------------------------------------------------------

class FakeTelegram:
    def __init__(self):
        self.inbox: list[dict] = []
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._msg_id = 1000
        self._update_id = 500

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


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """Момент по московскому времени, приведённый к UTC."""
    base = datetime.now(MSK).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).astimezone(timezone.utc)


def run_tick(tg, moment: datetime):
    """Один тик бота в заданный момент времени."""
    tick = srs.tick_of(moment)
    real_now, real_tick = store.now_utc, srs.tick_now
    for module in (store, srs, main, listen):
        module.now_utc = lambda _m=moment: _m
    srs.tick_now = lambda _m=None, _t=tick: _t
    try:
        state = store.load()
        main.process_updates(tg, state)
        for user in state["users"].values():
            main.deliver(tg, user, tick)
        store.save(state)
        return state
    finally:
        for module in (store, srs, main, listen):
            module.now_utc = real_now
        srs.tick_now = real_tick


def user_of(state):
    return state["users"][str(CHAT)]


# --- сценарий --------------------------------------------------------------

def run():
    tg = FakeTelegram()
    main.generate = fake_generate

    print("\n1. /start и выбор языка")
    tg.push_text("/start")
    run_tick(tg, at(0, 11))
    check("бот поздоровался", "Репитер" in tg.last())
    check("предложил выбор языка", tg.sent[-1]["keyboard"] == render.lang_keyboard())

    tg.push_callback("lang:en", tg.sent[-1]["message_id"])
    state = run_tick(tg, at(0, 11, 2))
    check("язык сохранён", user_of(state)["lang"] == "en")
    check("прислал инструкцию", "английский" in tg.last())

    print("\n2. Добавление слов")
    added_at = at(0, 11, 5)
    tg.push_text("serendipity\nquagmire, brouhaha")
    state = run_tick(tg, added_at)
    cards = store.active_cards(user_of(state))
    base = srs.tick_of(added_at)
    check("три карточки", len(cards) == 3, f"(получено {len(cards)})")
    check("перевод подтянулся", all(c["word_ru"] for c in cards))
    check("кеш примеров заполнен", all(len(c["cache"]) == 3 for c in cards))
    check("в подтверждении есть спойлер", "<tg-spoiler>" in tg.last())
    check("первый показ назначен на тик +2",
          all(c["due_tick"] == base + 2 for c in cards),
          f"({[c['due_tick'] - base for c in cards]})")

    print("\n3. Дубликаты не плодятся")
    tg.push_text("serendipity")
    state = run_tick(tg, at(0, 11, 10))
    check("осталось три карточки", len(store.active_cards(user_of(state))) == 3)
    check("бот сказал, что слово уже есть", "Уже в колоде" in tg.last())

    print("\n4. Ближайший тик пропускаем, показ — на следующем")
    before = len(tg.sent)
    run_tick(tg, added_at + timedelta(minutes=30))
    check("на тике +1 молчим", len(tg.sent) == before)
    run_tick(tg, added_at + timedelta(hours=1))
    fresh = tg.sent[before:]
    check("на тике +2 приходит слово", len(fresh) == 1, f"(пришло {len(fresh)})")
    reminder = fresh[0]
    check("слово выделено жирным", "<b>" in reminder["text"])
    check("перевод спрятан", "<tg-spoiler>" in reminder["text"])
    check("две кнопки", len(reminder["keyboard"][0]) == 2)
    check("кнопка «Я выучила»", reminder["keyboard"][0][0]["text"] == "✅ Я выучила")

    print("\n5. Не больше одного слова за тик")
    before = len(tg.sent)
    run_tick(tg, added_at + timedelta(hours=1, minutes=30))
    check("на следующем тике ровно одно", len(tg.sent) - before == 1)

    print("\n6. Повторный вызов того же тика ничего не шлёт")
    before = len(tg.sent)
    run_tick(tg, added_at + timedelta(hours=1, minutes=35))
    check("тик уже отработан — тишина", len(tg.sent) == before)

    print("\n7. Ночью бот молчит")
    state = store.load()
    for c in store.active_cards(user_of(state)):
        c["due_tick"] = srs.tick_of(at(1, 3)) - 1
    user_of(state)["sent_today"] = 0
    store.save(state)
    before = len(tg.sent)
    run_tick(tg, at(1, 3))
    check("в 3 ночи ни одного сообщения", len(tg.sent) == before)
    state = run_tick(tg, at(1, 10, 5))
    check("утром назревшее уходит", len(tg.sent) == before + 1)

    print("\n8. Кнопки двигают карточку по лесенке")
    card_id = reminder["keyboard"][0][0]["callback_data"].split(":", 1)[1]
    pressed = at(1, 12)
    tg.push_callback(f"ok:{card_id}", reminder["message_id"])
    state = run_tick(tg, pressed)
    card = store.find_card(user_of(state), card_id)
    check("шаг вырос до 1", card["step"] == 1, f"(шаг {card['step']})")
    check("следующий показ через 16 тиков — 8 часов",
          card["due_tick"] == srs.tick_of(pressed) + 16,
          f"(+{card['due_tick'] - srs.tick_of(pressed)})")
    check("клавиатура заменена на итог",
          "Выучено" in tg.edits[-1]["keyboard"][0][0]["text"])

    pressed = at(1, 12, 35)
    tg.push_callback(f"later:{card_id}", reminder["message_id"])
    state = run_tick(tg, pressed)
    card = store.find_card(user_of(state), card_id)
    check("«ещё хочу» вернул на шаг назад", card["step"] == 0)
    check("и назначил показ через 2 тика",
          card["due_tick"] == srs.tick_of(pressed) + 2)
    check("засчитан возврат", card["lapses"] == 1)

    print("\n9. Лесенка целиком")
    check("восемь ступеней", len(srs.LADDER) == 8)
    check("тик равен получасу", srs.TICK_SECONDS == 1800)
    check("8 часов = 16 тиков", srs.LADDER[1] == 8 * HOUR)
    check("сутки = 48 тиков", srs.LADDER[2] == 24 * HOUR)
    check("3 дня = 144 тика", srs.LADDER[3] == 3 * 24 * HOUR)
    check("неделя = 336 тиков", srs.LADDER[4] == 7 * 24 * HOUR)
    check("2 недели = 672 тика", srs.LADDER[5] == 14 * 24 * HOUR)
    check("месяц = 1440 тиков", srs.LADDER[6] == 30 * 24 * HOUR)
    check("3 месяца = 4320 тиков", srs.LADDER[7] == 90 * 24 * HOUR)
    check("интервалы растут",
          all(srs.LADDER[i] < srs.LADDER[i + 1] for i in range(7)))

    state = store.load()
    card = store.find_card(user_of(state), card_id)
    labels = [srs.on_known(card, now=1000) for _ in range(len(srs.LADDER))]
    store.save(state)
    check("карточка уходит в архив", card["archived"] is True)
    check("последний ярлык — архив", labels[-1] == "архив")
    check("у архивной карточки нет срока", card["due_tick"] is None)

    print("\n10. Нет примера — нет и сообщения")
    main.generate = failing_generate
    tg.push_text("kerfuffle")
    state = run_tick(tg, at(1, 13))
    kerfuffle = next(c for c in user_of(state)["cards"] if c["word"] == "kerfuffle")
    check("карточка всё равно создана", kerfuffle is not None)
    check("кеш пуст", kerfuffle["cache"] == [])

    state = store.load()
    user = user_of(state)
    for c in store.active_cards(user):
        c["due_tick"] = None
    kerfuffle = next(c for c in user["cards"] if c["word"] == "kerfuffle")
    kerfuffle["due_tick"] = srs.tick_of(at(1, 14))
    user["sent_today"] = 0
    store.save(state)

    before = len(tg.sent)
    state = run_tick(tg, at(1, 14))
    check("пустышку не шлём", len(tg.sent) == before)
    kerfuffle = next(c for c in user_of(state)["cards"] if c["word"] == "kerfuffle")
    check("карточка осталась назревшей", srs.is_due(kerfuffle, srs.tick_of(at(1, 14))))

    main.generate = fake_generate
    before = len(tg.sent)
    run_tick(tg, at(1, 14, 30))
    fresh = tg.sent[before:]
    check("следующим тиком сообщение уходит", len(fresh) == 1)
    check("в нём есть предложение со словом", "<b>kerfuffle</b>" in fresh[0]["text"])
    check("и спрятанный перевод", "<tg-spoiler>" in fresh[0]["text"])

    print("\n11. Дневной лимит")
    state = store.load()
    user = user_of(state)
    user["max_per_day"] = 2
    user["sent_today"] = 2
    user["sent_date"] = at(1, 15).astimezone(MSK).date().isoformat()
    for c in store.active_cards(user):
        c["due_tick"] = srs.tick_of(at(1, 15)) - 1
    store.save(state)
    before = len(tg.sent)
    run_tick(tg, at(1, 15))
    check("сверх лимита не шлём", len(tg.sent) == before)

    print("\n12. Команды")
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
        run_tick(tg, at(1, 16))
        replies = " ".join(m["text"] for m in tg.sent[before:])
        check(f"{cmd} отвечает", expect in replies, f"→ {replies[:70]!r}")

    state = store.load()
    check("окно применилось", user_of(state)["window"] == [9, 21])
    check("kerfuffle удалён",
          all(c["word"] != "kerfuffle" for c in user_of(state)["cards"]))

    print("\n13. Экранирование и жирный текст")
    html = render.bold_word("The <script> was pure serendipity & chaos",
                            ["serendipity"])
    check("угловые скобки экранированы", "&lt;script&gt;" in html)
    check("амперсанд экранирован", "&amp;" in html)
    check("слово выделено", "<b>serendipity</b>" in html)
    html2 = render.bold_word("She was quagmired again", ["quagmire"])
    check("нашлась изменённая форма", "<b>quagmired</b>" in html2, f"→ {html2!r}")

    print("\n14. Переезд со старого формата времени")
    old = {
        "id": "old1", "word": "vintage", "lang": "en", "word_ru": "", "step": 2,
        "reps": 1, "lapses": 0, "archived": False,
        "due_at": "2026-08-24T09:00:00+00:00",
        "send_at": "2026-08-24T10:00:00+00:00",
        "last_sent_at": None, "created_at": "2026-08-23T00:00:00+00:00",
    }
    migrated = store.migrate_card(dict(old))
    check("send_at превратился в номер тика",
          migrated["due_tick"] == srs.tick_of(store.parse(old["send_at"])))
    check("старые поля убраны",
          "send_at" not in migrated and "due_at" not in migrated)
    check("кеш и перевод на месте",
          migrated["cache"] == [] and migrated["word_ru"] == "")

    print("\n15. Живой цикл отвечает сразу")
    tg.push_text("/stats")
    before = len(tg.sent)
    code = listen.run(tg, runtime=0.4)
    check("цикл завершился штатно", code == 0)
    replies = " ".join(m["text"] for m in tg.sent[before:])
    check("ответ пришёл в том же цикле", "Статистика" in replies)
    check("длинный опрос включён", listen.POLL_TIMEOUT > 0)
    check("смена короче лимита job-а в 6 часов", listen.DEFAULT_RUNTIME < 6 * 3600)

    print("\n16. Что бот НЕ должен говорить")
    everything = " ".join(m["text"] for m in tg.sent)
    everything += " " + " ".join(
        b["text"] for m in tg.sent for row in (m["keyboard"] or []) for b in row
    )
    everything += " " + " ".join(
        b["text"] for e in tg.edits for row in (e["keyboard"] or []) for b in row
    )
    everything = everything.lower()
    for word in ["grok", "грок", "gpt", "нейросет", "нейронк", " ии ", "openai", "xai"]:
        check(f"не упоминает «{word.strip()}»", word not in everything)
    # по границам слова, иначе «статистика» ловится на «тик»
    for word in ["тик", "тика", "тики", "тиков", "расписание", "полчаса", "интервал"]:
        check(f"не упоминает «{word}»",
              re.search(rf"\b{word}\b", everything) is None)
    for phrase in ["через 8 часов", "через 16", "завтра", "через 3 дня",
                   "через сутки", "через неделю", "через месяц"]:
        check(f"не выдаёт время показа «{phrase}»", phrase not in everything)

    print(f"\n{'─' * 46}")
    print(f"Пройдено: {CHECKS['ok']}   Провалено: {CHECKS['fail']}")
    return 0 if CHECKS["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
