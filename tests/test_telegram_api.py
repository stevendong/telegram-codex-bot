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

    async def test_inline_keyboard_and_callback_methods(self) -> None:
        api = _RecordingTelegramAPI()
        markup = {
            "inline_keyboard": [
                [{"text": "Switch", "callback_data": "agent:main"}]
            ]
        }

        await api.send_message(10, "Choose", reply_markup=markup)
        await api.edit_message_text(10, 20, "Selected", reply_markup=markup)
        await api.answer_callback_query("query-1", "Done")

        self.assertEqual(api.calls[0][0], "sendMessage")
        self.assertEqual(api.calls[0][1]["reply_markup"], markup)
        self.assertEqual(api.calls[1][0], "editMessageText")
        self.assertEqual(api.calls[1][1]["message_id"], 20)
        self.assertEqual(api.calls[2][0], "answerCallbackQuery")
        self.assertEqual(api.calls[2][1]["callback_query_id"], "query-1")
