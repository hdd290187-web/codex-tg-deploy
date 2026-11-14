from __future__ import annotations

from typing import Any

import httpx

from .security import redact


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self.token = token
        self.client = client
        self.base = f"https://api.telegram.org/bot{token}"

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.client.post(f"{self.base}/{method}", json=payload or {})
            data = response.json()
        except Exception as exc:
            raise TelegramError(redact(f"Telegram network error: {exc}", self.token)) from exc
        if response.status_code >= 400 or not data.get("ok"):
            description = redact(str(data.get("description", "Telegram request failed")), self.token)
            raise TelegramError(description)
        return data.get("result")

    async def prepare_binding(self) -> tuple[dict[str, Any], int, bool]:
        bot = await self.call("getMe")
        webhook = await self.call("getWebhookInfo")
        removed = bool(webhook.get("url"))
        if removed:
            await self.call("deleteWebhook", {"drop_pending_updates": False})
        updates = await self.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        offset = max((int(item["update_id"]) for item in updates), default=-1) + 1
        return bot, offset, removed

    async def poll_start(self, offset: int) -> tuple[int, int | None, int | None]:
        updates = await self.call(
            "getUpdates", {"offset": offset, "timeout": 1, "allowed_updates": ["message"]}
        )
        next_offset = offset
        user_id = None
        chat_id = None
        for update in updates:
            next_offset = max(next_offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            text = str(message.get("text", ""))
            if chat.get("type") == "private" and text.split(maxsplit=1)[0].startswith("/start"):
                user_id = int(sender["id"])
                chat_id = int(chat["id"])
        return next_offset, user_id, chat_id
