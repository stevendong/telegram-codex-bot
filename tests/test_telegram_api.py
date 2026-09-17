from __future__ import annotations

import unittest
from typing import Any

from telegram_codex_bot.bot import BOT_COMMANDS
from telegram_codex_bot.telegram_api import TelegramAPI, TelegramError


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


class _FailingRichTelegramAPI(_RecordingTelegramAPI):
    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        result = await super().call(method, payload, timeout=timeout)
        if method == "sendRichMessage":
            raise TelegramError("rich markdown rejected")
        return result


class _FailingFormattedTelegramAPI(_RecordingTelegramAPI):
    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        result = await super().call(method, payload, timeout=timeout)
        if method == "sendRichMessage" or (payload or {}).get("parse_mode"):
            raise TelegramError("formatted message rejected")
        return result


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

    async def test_sends_native_rich_markdown(self) -> None:
        api = _RecordingTelegramAPI()

        await api.send_rich_markdown(10, "# Result\n\n```python\nprint('ok')\n```")

        self.assertEqual(len(api.calls), 1)
        method, payload = api.calls[0]
        self.assertEqual(method, "sendRichMessage")
        self.assertEqual(payload["chat_id"], 10)
        self.assertIn("# Result", payload["rich_message"]["markdown"])

    async def test_rich_markdown_falls_back_to_html(self) -> None:
        api = _FailingRichTelegramAPI()

        with self.assertLogs("telegram_codex_bot.telegram_api", level="WARNING"):
            await api.send_rich_markdown(10, "# Result\n\n**done**")

        self.assertEqual(
            [method for method, _ in api.calls],
            ["sendRichMessage", "sendMessage"],
        )
        fallback = api.calls[1][1]
        self.assertEqual(fallback["parse_mode"], "HTML")
        self.assertIn("<b>Result</b>", fallback["text"])
        self.assertIn("<b>done</b>", fallback["text"])

    async def test_formatted_failures_fall_back_to_plain_text(self) -> None:
        api = _FailingFormattedTelegramAPI()

        with self.assertLogs("telegram_codex_bot.telegram_api", level="WARNING"):
            await api.send_rich_markdown(10, "# Result\n\n**done**")

        self.assertEqual(
            [method for method, _ in api.calls],
            ["sendRichMessage", "sendMessage", "sendMessage"],
        )
        plain = api.calls[-1][1]
        self.assertNotIn("parse_mode", plain)
        self.assertEqual(plain["text"], "# Result\n\n**done**")
