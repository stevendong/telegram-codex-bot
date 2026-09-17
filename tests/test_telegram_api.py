from __future__ import annotations

import unittest
from typing import Any

from telegram_codex_bot.bot import BOT_COMMANDS
from telegram_codex_bot.telegram_api import TelegramAPI


class _RecordingTelegramAPI(TelegramAPI):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        del timeout
        self.calls.append((method, payload or {}))
        return True


class TelegramAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_configure_command_menu_for_private_chats(self) -> None:
        api = _RecordingTelegramAPI()
        await api.configure_command_menu(BOT_COMMANDS)

        self.assertEqual(api.calls[0][0], "setMyCommands")
        self.assertEqual(
            api.calls[0][1]["scope"], {"type": "all_private_chats"}
        )
        self.assertEqual(api.calls[0][1]["commands"], BOT_COMMANDS)
        self.assertEqual(
            api.calls[1],
            (
                "setChatMenuButton",
                {"menu_button": {"type": "commands"}},
            ),
        )
