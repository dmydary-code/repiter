"""Сборка текстов сообщений (parse_mode=HTML)."""

from __future__ import annotations

import re

from .tg import esc

LANG_TITLE = {"en": "английский", "fr": "французский"}
LANG_FLAG = {"en": "🇬🇧", "fr": "🇫🇷"}


def bold_word(sentence: str, forms: list[str]) -> str:
    """Экранирует предложение и выделяет жирным изучаемое слово."""
    safe = esc(sentence)
    for form in forms:
        form = (form or "").strip()
        if not form:
            continue
        # \w* по краям — чтобы жирным выделилось слово целиком, даже если
        # в предложении оно стоит в другой форме: quagmire → quagmired.
        pattern = re.compile(rf"\w*{re.escape(esc(form))}\w*", re.IGNORECASE)
        result, hits = pattern.subn(lambda m: f"<b>{m.group(0)}</b>", safe)
        if hits:
            return result

    # Слово могло изменить форму — пробуем по корню.
    root = next((f for f in forms if f), "")
    if len(root) >= 4:
        stem = esc(root[: max(4, len(root) - 2)])
        pattern = re.compile(rf"\b{re.escape(stem)}\w*", re.IGNORECASE)
        result, hits = pattern.subn(lambda m: f"<b>{m.group(0)}</b>", safe)
        if hits:
            return result
    return safe


def reminder(card: dict, example: dict) -> str:
    """Напоминание. Всегда содержит живое предложение со словом и спрятанный
    перевод — без примера сообщение просто не отправляется."""
    word = esc(card["word"])
    word_ru = card.get("word_ru") or "?"
    sentence = bold_word(example["sentence"], [example.get("word_form", ""), card["word"]])

    lines = [f"🧠 <b>{word}</b>", "", sentence, "", f"🙈 <tg-spoiler>{esc(word_ru)}</tg-spoiler>"]
    if example.get("sentence_ru"):
        lines.append(f"💬 <tg-spoiler>{esc(example['sentence_ru'])}</tg-spoiler>")
    return "\n".join(lines)


def reminder_keyboard(card_id: str) -> list[list[dict]]:
    return [
        [
            {"text": "✅ Я выучила", "callback_data": f"ok:{card_id}"},
            {"text": "🔁 Ещё хочу потом", "callback_data": f"later:{card_id}"},
        ]
    ]


def answered_keyboard(known: bool, archived: bool = False) -> list[list[dict]]:
    # Когда прилетит следующий показ — не говорим: половина пользы приёма
    # в том, что повторение застаёт врасплох.
    if archived:
        text = "🏆 Слово закрыто"
    elif known:
        text = "✅ Выучено"
    else:
        text = "🔁 Вернём ещё разок"
    return [[{"text": text, "callback_data": "noop"}]]


def lang_keyboard() -> list[list[dict]]:
    return [
        [
            {"text": "🇬🇧 Английский", "callback_data": "lang:en"},
            {"text": "🇫🇷 Французский", "callback_data": "lang:fr"},
        ]
    ]


def welcome() -> str:
    return (
        "Привет! Я <b>Репитер</b> 🦜\n"
        "Ты кидаешь мне слова, я раскидываю их по дню и подсовываю в смешных "
        "предложениях, пока они не осядут в голове.\n\n"
        "Какой язык учим?"
    )


def language_set(lang: str) -> str:
    return (
        f"Отлично, учим {LANG_FLAG.get(lang, '')} <b>{LANG_TITLE.get(lang, lang)}</b>.\n\n"
        "Теперь просто пришли мне слово — или сразу несколько, каждое с новой строки.\n\n"
        "Дальше я буду подкидывать его в случайные моменты дня, каждый раз "
        "в новом предложении. Под каждым две кнопки: "
        "<b>Я выучила</b> растягивает паузу до следующей встречи, "
        "<b>Ещё хочу потом</b> — наоборот, сокращает.\n\n"
        "/help — что я ещё умею."
    )


def help_text(user: dict) -> str:
    start, end = user.get("window", [10, 22])
    lang = LANG_TITLE.get(user.get("lang"), "не выбран")
    return (
        "<b>Что я умею</b>\n\n"
        "Просто напиши слово — добавлю в колоду. Несколько слов — каждое с новой строки.\n\n"
        "/list — что сейчас в работе\n"
        "/stats — статистика\n"
        "/lang — сменить язык\n"
        "/window 10 22 — часы, в которые можно писать\n"
        "/limit 8 — максимум напоминаний в день\n"
        "/delete слово — убрать слово\n"
        "/pause и /resume — тишина и обратно\n"
        "/help — это сообщение\n\n"
        f"<i>Сейчас: язык — {lang}, окно — {start}:00–{end}:00, "
        f"лимит — {user.get('max_per_day', 8)} в день.</i>"
    )


def card_list(user: dict, cards: list[dict]) -> str:
    """Список слов с прогрессом. Без времени следующего показа — сюрприз
    входит в методику."""
    if not cards:
        return "Колода пустая. Пришли мне слово — начнём."
    rows = []
    for card in sorted(cards, key=lambda c: (-c["step"], c["word"].lower())):
        dots = "●" * (card["step"] + 1) + "○" * (7 - card["step"])
        rows.append(f"<code>{dots}</code>  {esc(card['word'])}")
    return f"<b>В работе — {len(cards)}</b>\n\n" + "\n".join(rows)


def stats(user: dict) -> str:
    cards = user["cards"]
    active = [c for c in cards if not c["archived"]]
    done = [c for c in cards if c["archived"]]
    reps = sum(c["reps"] for c in cards)
    lapses = sum(c["lapses"] for c in cards)
    fresh = [c for c in active if c["step"] <= 1]
    solid = [c for c in active if c["step"] >= 5]
    return (
        "<b>Статистика</b>\n\n"
        f"🏆 Закрыто: <b>{len(done)}</b>\n"
        f"📚 В работе: <b>{len(active)}</b>\n"
        f"🌱 Из них совсем свежих: {len(fresh)}\n"
        f"💪 Почти выучено: {len(solid)}\n\n"
        f"✅ Нажатий «выучила»: {reps}\n"
        f"🔁 Возвратов назад: {lapses}"
    )
