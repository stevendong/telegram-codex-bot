from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any


class TelegramError(RuntimeError):
    pass


def split_text(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if not text:
        return ["（空消息）"]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        return await asyncio.to_thread(
            self._call_sync, method, payload or {}, timeout
        )

    def _call_sync(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise TelegramError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Telegram connection failed: {exc.reason}") from exc
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or "Telegram API failed"))
        return data.get("result")

    async def get_updates(self, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=poll_timeout + 15,
        )
        return list(result or [])

    async def configure_command_menu(
        self, commands: list[dict[str, str]]
    ) -> None:
        await self.call(
            "setMyCommands",
            {
                "commands": commands,
                "scope": {"type": "all_private_chats"},
            },
        )
        await self.call(
            "setChatMenuButton",
            {"menu_button": {"type": "commands"}},
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_text(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            await self.call(
                "sendMessage",
                payload,
            )

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        await self.call("answerCallbackQuery", payload)

    async def send_typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
