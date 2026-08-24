"""Генерация мемных примеров.

По умолчанию — Gemini Flash-Lite через её **нативный** эндпоинт с заголовком
`x-goog-api-key`. Это важно: ключи нового формата (`AQ.…`, именно такие
выдаёт AI Studio с 2026 года) на OpenAI-совместимом пути через
`Authorization: Bearer` отвечают «Please pass a valid API key», хотя сами
исправны. Старые `AIza…` работали и там, но их отключают.

Провайдера можно сменить, не трогая код: переменные репозитория LLM_API_URL
и LLM_MODEL плюс секрет LLM_API_KEY. Если в LLM_API_URL указан любой другой
адрес, запрос уходит в формате OpenAI (`Authorization: Bearer`), так что
подходит почти любой сторонний сервис.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_HOST = "generativelanguage.googleapis.com"

# Запасные имена моделей на случай, если основная отвечает 404.
FALLBACKS = {
    GEMINI_HOST: [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ],
    "api.x.ai": ["grok-4.6", "grok-4-latest", "grok-3-mini"],
}

_active_model: str | None = None


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def api_url() -> str:
    return _env("LLM_API_URL", "XAI_API_URL") or GEMINI_NATIVE


def api_key() -> str:
    return _env("LLM_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY")


def is_gemini_native(url: str | None = None) -> bool:
    """Нативный путь Gemini — всё, что ведёт на её хост мимо /openai/."""
    url = url if url is not None else api_url()
    return GEMINI_HOST in url and "/openai/" not in url


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


# Google выдаёт ключи двух поколений: старые AIza… и новые AQ.…
GOOGLE_KEY = re.compile(r"^(AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-.]{20,})$")


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
    if GEMINI_HOST in api_url() and not GOOGLE_KEY.match(key):
        return (
            "ключ не похож на ключ Google AI Studio: такие начинаются с AQ. "
            f"или AIza. В секрете сейчас {len(key)} символов. Заведи ключ на "
            "aistudio.google.com/apikey и перезапиши секрет LLM_API_KEY"
        )
    return None


HINTS = {
    400: (
        "сервис не принял запрос. Если в тексте ниже «Please pass a valid API key», "
        "а ключ заведомо рабочий — значит запрос ушёл не тем путём: ключи AQ. "
        "живут только на нативном эндпоинте Gemini. Проверь, не переопределена ли "
        "переменная LLM_API_URL"
    ),
    401: "ключ не принят — проверь, что секрет скопирован целиком",
    403: (
        "ключ валиден, но доступ закрыт: он может быть ограничен по адресам "
        "или в проекте не включён Generative Language API"
    ),
    404: (
        "модель не найдена. Задай переменную репозитория LLM_MODEL "
        "(Settings → Secrets and variables → Actions → Variables)"
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


# --------------------------------------------------------------------------
# Транспорт
# --------------------------------------------------------------------------

def _post(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last, last_status = None, None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")[:400]
            last, last_status = f"HTTP {e.code}: {body_text}", e.code
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last, last_status = f"сеть: {e}", None
            time.sleep(2 * (attempt + 1))
    raise GenerationFailed(last or "unknown", last_status)


def _call_gemini(model: str, prompt: str, key: str, temperature: str | None) -> str:
    """Нативный вызов. Ключ идёт заголовком, а не в строке запроса — в URL
    секретам не место."""
    config: dict = {"responseMimeType": "application/json"}
    if temperature:
        config["temperature"] = float(temperature)
    raw = _post(
        f"{api_url().rstrip('/')}/{model}:generateContent",
        {
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        },
        {"x-goog-api-key": key},
    )
    try:
        candidate = raw["candidates"][0]
        return "".join(p.get("text", "") for p in candidate["content"]["parts"])
    except (KeyError, IndexError) as e:
        reason = (raw.get("candidates") or [{}])[0].get("finishReason", "")
        raise GenerationFailed(
            f"пустой ответ{f' ({reason})' if reason else ''}: {str(raw)[:200]}"
        ) from e


def _call_openai(model: str, prompt: str, key: str, temperature: str | None) -> str:
    """Формат OpenAI — для всех прочих сервисов."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    if temperature:
        body["temperature"] = float(temperature)
    try:
        raw = _post(api_url(), body, {"Authorization": f"Bearer {key}"})
    except GenerationFailed as e:
        # Не все модели принимают строгий JSON-режим. Просить JSON словами
        # мы и так умеем — парсер выдержит.
        if e.status == 400 and "response_format" in str(e):
            print(f"[api] {model} не принимает json_object, повторяю без него")
            body.pop("response_format")
            raw = _post(api_url(), body, {"Authorization": f"Bearer {key}"})
        else:
            raise
    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise GenerationFailed(f"неожиданный ответ: {str(raw)[:200]}") from e


def _complete(prompt: str, key: str) -> str:
    """Пробует модели по очереди. Переключается только на 404 — остальные
    ошибки сменой модели не лечатся."""
    global _active_model

    temperature = _env("LLM_TEMPERATURE", "XAI_TEMPERATURE") or None
    call = _call_gemini if is_gemini_native() else _call_openai
    last_error: GenerationFailed | None = None

    for name in model_candidates():
        try:
            text = call(name, prompt, key, temperature)
        except GenerationFailed as e:
            last_error = e
            if e.status == 404:
                print(f"[api] модель {name} недоступна, пробую следующую")
                continue
            raise
        if _active_model != name:
            print(f"[api] работаю на модели {name}")
            _active_model = name
        return text

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
    parsed = _extract_json(_complete(prompt, key))
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
