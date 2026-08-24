"""Генерация мемных примеров через xAI Grok API (OpenAI-совместимый эндпоинт)."""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("XAI_API_URL", "https://api.x.ai/v1/chat/completions")
MODEL = os.environ.get("XAI_MODEL", "grok-4.6")

# Если основная модель отвечает 404 (переименовали, нет доступа на тарифе),
# пробуем соседние и запоминаем ту, что ответила.
FALLBACKS = ["grok-4.6", "grok-4-latest", "grok-4", "grok-3", "grok-3-mini"]
_active_model: str | None = None

LANG_NAMES = {"en": "английском", "fr": "французском"}
LANG_LABEL = {"en": "английский", "fr": "французский"}

# Подмешиваем случайный жанр — так примеры не скатываются в один и тот же шаблон.
STYLES = [
    "абсурдная бытовая сценка",
    "формат интернет-мема",
    "драматичный внутренний монолог",
    "объявление на подъезде",
    "отзыв на маркетплейсе",
    "сообщение от бывшего в три часа ночи",
    "новостной заголовок из параллельной вселенной",
    "жалоба в чат дома",
    "реплика из сериала про офис",
    "гороскоп на завтра",
    "подпись под фото в соцсетях",
    "разговор кота с хозяйкой",
]

SYSTEM = """Ты придумываешь мнемонические примеры для запоминания иностранных слов.
Твои примеры смешные, абсурдные, чуть-чуть дерзкие — такие, что западают в память.
Ты не морализируешь, не пишешь скучных учебниковых фраз и не объясняешь шутку.
Отвечаешь всегда строго валидным JSON без markdown-обёртки."""

USER_TEMPLATE = """Слово на {lang_name} языке: "{word}"

Придумай {count} шт. коротких примеров (каждое предложение — до 18 слов) на {lang_name} языке,
где это слово употреблено естественно и грамматически верно.
Жанр для вдохновения: {style}. Примеры должны быть разными по смыслу.

Верни JSON такого вида:
{{
  "word_ru": "перевод слова на русский (1-3 варианта через запятую)",
  "examples": [
    {{
      "sentence": "предложение на {lang_name} языке",
      "word_form": "точная форма слова так, как она написана в предложении",
      "sentence_ru": "перевод всего предложения на русский"
    }}
  ]
}}

Никакого текста вне JSON."""


class GrokUnavailable(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(payload: dict, api_key: str, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    last_status = None
    for attempt in range(3):
        req = urllib.request.Request(
            API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            last = f"HTTP {e.code}: {body}"
            last_status = e.code
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last = f"сеть: {e}"
            last_status = None
            time.sleep(2 * (attempt + 1))
    raise GrokUnavailable(last or "unknown", last_status)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise GrokUnavailable(f"не JSON: {text[:200]}") from None
        return json.loads(match.group(0))


HINTS = {
    401: (
        "ключ не принят. Проверь, что секрет XAI_API_KEY скопирован целиком, "
        "без пробелов и переносов, и что это именно API-ключ, а не что-то другое"
    ),
    403: (
        "ключ валиден, но доступ закрыт. Самая частая причина — на счету xAI "
        "нет средств: бесплатных кредитов там нет, биллинг нужно пополнить. "
        "Вторая по частоте — ключ выпущен для другой команды (team) "
        "или ему не выданы права на chat completions"
    ),
    404: (
        "модель не найдена. Задай переменную репозитория XAI_MODEL "
        "(Settings → Secrets and variables → Actions → Variables) "
        "с именем модели, доступной твоему аккаунту"
    ),
    400: "сервис не принял запрос — смотри текст ответа ниже",
    429: "упёрлись в лимит запросов, стоит подождать",
}


def diagnose() -> str:
    """Одна строка для лога Actions: работает генерация примеров или нет,
    и если нет — почему именно."""
    if not os.environ.get("XAI_API_KEY", "").strip():
        return (
            "ключ XAI_API_KEY не виден воркфлоу. Проверь имя секрета "
            "(ровно XAI_API_KEY) и что он проброшен в env нужного шага"
        )
    try:
        data = generate("serendipity", "en", count=1)
    except GrokUnavailable as e:
        hint = HINTS.get(e.status or 0, "неожиданный ответ сервиса")
        return f"НЕ РАБОТАЕТ — {hint}\n  ответ сервиса: {e}"
    return f"работает, модель {_active_model}, пример: {data['examples'][0]['sentence'][:70]}"


def _candidates() -> list[str]:
    if _active_model:
        return [_active_model]
    seen, out = set(), []
    for name in [MODEL, *FALLBACKS]:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _request_with_fallback(prompt: str, api_key: str) -> dict:
    """Пробует модели по очереди. Переключается только на 404 — остальные
    ошибки (нет ключа, нет средств, лимит) от смены модели не лечатся."""
    global _active_model

    temperature = os.environ.get("XAI_TEMPERATURE")
    last_error: GrokUnavailable | None = None

    for name in _candidates():
        payload = {
            "model": name,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        if temperature:
            payload["temperature"] = float(temperature)
        try:
            raw = _request(payload, api_key)
        except GrokUnavailable as e:
            last_error = e
            if e.status == 404:
                print(f"[api] модель {name} недоступна, пробую следующую")
                continue
            # Некоторые модели не принимают строгий JSON-режим. Просить JSON
            # словами мы и так умеем — парсер выдержит.
            if e.status == 400 and "response_format" in str(e):
                print(f"[api] {name} не принимает json_object, повторяю без него")
                payload.pop("response_format")
                try:
                    raw = _request(payload, api_key)
                except GrokUnavailable as e2:
                    last_error = e2
                    continue
                if _active_model != name:
                    print(f"[api] работаю на модели {name}")
                    _active_model = name
                return raw
            raise
        if _active_model != name:
            print(f"[api] работаю на модели {name}")
            _active_model = name
        return raw

    raise last_error or GrokUnavailable("ни одна модель не ответила")


def generate(word: str, lang: str, count: int = 1, api_key: str | None = None) -> dict:
    """Возвращает {"word_ru": str, "examples": [{sentence, word_form, sentence_ru}]}.

    Бросает GrokUnavailable, если API недоступен или ответ не разобрать.
    """
    api_key = api_key or os.environ.get("XAI_API_KEY", "")
    if not api_key:
        raise GrokUnavailable("XAI_API_KEY не задан")

    prompt = USER_TEMPLATE.format(
        lang_name=LANG_NAMES.get(lang, "английском"),
        word=word,
        count=count,
        style=random.choice(STYLES),
    )
    raw = _request_with_fallback(prompt, api_key)
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise GrokUnavailable(f"неожиданный ответ: {str(raw)[:200]}") from e

    parsed = _extract_json(content)
    examples = [
        {
            "sentence": str(ex.get("sentence", "")).strip(),
            "word_form": str(ex.get("word_form", word)).strip(),
            "sentence_ru": str(ex.get("sentence_ru", "")).strip(),
        }
        for ex in parsed.get("examples", [])
        if str(ex.get("sentence", "")).strip()
    ]
    if not examples:
        raise GrokUnavailable("пустой список примеров")
    return {"word_ru": str(parsed.get("word_ru", "")).strip(), "examples": examples}
