"""Минимальный клиент Telegram Bot API на стандартной библиотеке.

Без зависимостей — чтобы GitHub Actions не тратил время на pip install.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_URL = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, timeout: int = 30):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN пуст")
        self.token = token
        self.timeout = timeout

    # --- низкий уровень -------------------------------------------------

    def call(self, method: str, **params) -> dict:
        """Вызов метода API. Возвращает распарсенный ответ Telegram.

        Никогда не бросает исключение на ошибках API — возвращает
        {"ok": False, ...}, чтобы один сбойный запрос не ронял весь тик.
        """
        url = API_URL.format(token=self.token, method=method)
        payload = json.dumps(params, ensure_ascii=False).encode("utf-8")

        last_err = None
        for attempt in range(3):
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = {"ok": False, "description": body}
                # 429 — притормозить и повторить
                if e.code == 429:
                    retry_after = (parsed.get("parameters") or {}).get("retry_after", 3)
                    time.sleep(min(retry_after, 20))
                    last_err = parsed
                    continue
                return parsed
            except Exception as e:  # noqa: BLE001 — сеть/таймаут
                last_err = {"ok": False, "description": f"network: {e}"}
                time.sleep(1.5 * (attempt + 1))

        return last_err or {"ok": False, "description": "unknown"}

    # --- удобные обёртки ------------------------------------------------

    def get_updates(self, offset: int, limit: int = 100) -> list[dict]:
        res = self.call(
            "getUpdates",
            offset=offset,
            limit=limit,
            timeout=0,
            allowed_updates=["message", "callback_query"],
        )
        if not res.get("ok"):
            print(f"[tg] getUpdates failed: {res.get('description')}")
            return []
        return res.get("result", [])

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict]] | None = None,
        preview: bool = False,
    ) -> dict:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
        }
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict]] | None = None,
    ) -> dict:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        params["reply_markup"] = {"inline_keyboard": keyboard or []}
        return self.call("editMessageText", **params)

    def edit_reply_markup(
        self, chat_id: int, message_id: int, keyboard: list[list[dict]] | None = None
    ) -> dict:
        return self.call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup={"inline_keyboard": keyboard or []},
        )

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        # Часто падает с "query is too old" — это нормально, тик мог
        # проснуться через 15 минут после нажатия. Ошибку глотаем.
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def set_my_commands(self, commands: list[dict]) -> dict:
        return self.call("setMyCommands", commands=commands)


def esc(text: str) -> str:
    """Экранирование для parse_mode=HTML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
