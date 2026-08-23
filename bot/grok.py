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
    pass


def _request(payload: dict, api_key: str, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
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
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last = f"network: {e}"
            time.sleep(2 * (attempt + 1))
    raise GrokUnavailable(last or "unknown")


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
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    temperature = os.environ.get("XAI_TEMPERATURE")
    if temperature:
        payload["temperature"] = float(temperature)

    raw = _request(payload, api_key)
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
