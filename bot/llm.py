"""Генерация мемных примеров через OpenAI-совместимый эндпоинт.

По умолчанию — Gemini Flash-Lite: у него бесплатный тариф с запасом
покрывает нагрузку бота. Провайдера можно сменить, не трогая код:
переменные репозитория LLM_API_URL и LLM_MODEL плюс секрет LLM_API_KEY.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
XAI_URL = "https://api.x.ai/v1/chat/completions"

# Запасные имена моделей на случай, если основная отвечает 404.
FALLBACKS = {
    "generativelanguage.googleapis.com": [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ],
    "api.x.ai": ["grok-4.6", "grok-4-latest", "grok-3-mini"],
}


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def api_url() -> str:
    return _env("LLM_API_URL", "XAI_API_URL") or GEMINI_URL


def api_key() -> str:
    return _env("LLM_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY")


def _host(url: str) -> str:
    for host in FALLBACKS:
        if host in url:
            return host
    return ""


def model_candidates() -> list[str]:
    if _active_model:
        return [_active_model]
    configured = _env("LLM_MODEL", "XAI_MODEL")
    fallbacks = FALLBACKS.get(_host(api_url()), [])
    seen, out = set(), []
    for name in [configured, *fallbacks]:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out or ["gemini-3.5-flash-lite"]


_active_model: str | None = None

LANG_NAMES = {"en": "английском", "fr": "французском"}

# Подмешиваем случайный жанр — так примеры не скатываются в один шаблон.
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


class GenerationFailed(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


GOOGLE_KEY = re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")


def key_problem() -> str | None:
    """Беглая проверка ключа до запроса. Само значение никуда не печатаем:
    лог публичного репозитория — не место для секретов."""
    key = api_key()
    if not key:
        return None
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return (
            "в ключе есть символы вне латиницы — похоже, при копировании "
            "прилипло лишнее. Пересохрани секрет LLM_API_KEY"
        )
    if "generativelanguage.googleapis.com" in api_url() and not GOOGLE_KEY.match(key):
        prefix = "есть" if key.startswith("AIza") else "нет"
        return (
            "ключ не похож на ключ Google AI Studio. Такие начинаются с AIza "
            f"и состоят из ~39 латинских символов. В секрете сейчас {len(key)} "
            f"символов, префикс AIza — {prefix}. Заведи ключ на "
            "aistudio.google.com/apikey и перезапиши секрет LLM_API_KEY"
        )
    return None


HINTS = {
    400: "сервис не принял запрос — смотри текст ответа ниже",
    401: (
        "ключ не принят. Проверь, что секрет LLM_API_KEY скопирован целиком, "
        "без пробелов и переносов"
    ),
    403: (
        "ключ валиден, но доступ закрыт. У Gemini так бывает, если ключ ограничен "
        "по адресам или в проекте не включён Generative Language API. "
        "У платных сервисов — если на счету нет средств"
    ),
    404: (
        "модель не найдена. Задай переменную репозитория LLM_MODEL "
        "(Settings → Secrets and variables → Actions → Variables) "
        "с актуальным именем модели"
    ),
    429: "упёрлись в лимит бесплатного тарифа, стоит подождать",
}


def diagnose() -> str:
    """Одна строка для лога: работает генерация примеров или нет, и почему."""
    if not api_key():
        return (
            "ключ не виден воркфлоу. Нужен секрет LLM_API_KEY "
            "(Settings → Secrets and variables → Actions → Secrets)"
        )
    problem = key_problem()
    if problem:
        return f"НЕ РАБОТАЕТ — {problem}"
    try:
        data = generate("serendipity", "en", count=1)
    except GenerationFailed as e:
        hint = HINTS.get(e.status or 0, "неожиданный ответ сервиса")
        return f"НЕ РАБОТАЕТ — {hint}\n  ответ сервиса: {e}"
    return (
        f"работает, модель {_active_model}, "
        f"пример: {data['examples'][0]['sentence'][:70]}"
    )


def _request(payload: dict, key: str, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last, last_status = None, None
    for attempt in range(3):
        req = urllib.request.Request(
            api_url(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            last, last_status = f"HTTP {e.code}: {body}", e.code
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last, last_status = f"сеть: {e}", None
            time.sleep(2 * (attempt + 1))
    raise GenerationFailed(last or "unknown", last_status)


def _request_with_fallback(prompt: str, key: str) -> dict:
    """Пробует модели по очереди. Переключается только на 404 — остальные
    ошибки (нет ключа, нет доступа, лимит) сменой модели не лечатся."""
    global _active_model

    temperature = os.environ.get("LLM_TEMPERATURE") or os.environ.get("XAI_TEMPERATURE")
    last_error: GenerationFailed | None = None

    for name in model_candidates():
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
            raw = _request(payload, key)
        except GenerationFailed as e:
            last_error = e
            if e.status == 404:
                print(f"[api] модель {name} недоступна, пробую следующую")
                continue
            # Не все модели принимают строгий JSON-режим. Просить JSON словами
            # мы и так умеем — парсер выдержит.
            if e.status == 400 and "response_format" in str(e):
                print(f"[api] {name} не принимает json_object, повторяю без него")
                payload.pop("response_format")
                try:
                    raw = _request(payload, key)
                except GenerationFailed as e2:
                    last_error = e2
                    continue
            else:
                raise
        if _active_model != name:
            print(f"[api] работаю на модели {name}")
            _active_model = name
        return raw

    raise last_error or GenerationFailed("ни одна модель не ответила")


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise GenerationFailed(f"не JSON: {text[:200]}") from None
        return json.loads(match.group(0))


def generate(word: str, lang: str, count: int = 1, key: str | None = None) -> dict:
    """Возвращает {"word_ru": str, "examples": [{sentence, word_form, sentence_ru}]}.

    Бросает GenerationFailed, если сервис недоступен или ответ не разобрать.
    """
    from_env = key is None
    key = key or api_key()
    if not key:
        raise GenerationFailed("ключ не задан")
    if from_env:
        problem = key_problem()
        if problem:
            raise GenerationFailed(problem, 400)

    prompt = USER_TEMPLATE.format(
        lang_name=LANG_NAMES.get(lang, "английском"),
        word=word,
        count=count,
        style=random.choice(STYLES),
    )
    raw = _request_with_fallback(prompt, key)
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise GenerationFailed(f"неожиданный ответ: {str(raw)[:200]}") from e

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
        raise GenerationFailed("пустой список примеров")
    return {"word_ru": str(parsed.get("word_ru", "")).strip(), "examples": examples}
