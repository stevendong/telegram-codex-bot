from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_codex_bot.bot import TelegramCodexBot
from telegram_codex_bot.state import StateStore
from telegram_codex_bot.telegram_api import TelegramAPI


class _FakeService:
    def __init__(self) -> None:
        self.usage_requests: list[str] = []

    def account_names(self) -> list[str]:
        return ["default"]

    def account_aliases(self) -> dict[str, str]:
        return {"default": "main"}

    def account_alias(self, account: str) -> str:
        return "main" if account == "default" else account

    async def get_account_usage(self, account: str) -> dict[str, Any]:
        self.usage_requests.append(account)
        return {
            "primary": {
                "usedPercent": 20,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_000,
            },
            "secondary": {"usedPercent": 55, "windowDurationMins": 10080},
        }


class _FakeModelService(_FakeService):
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.selected: list[tuple[int, str, str]] = []

    async def list_models(self, account: str) -> list[dict[str, Any]]:
        del account
        return [
            {
                "model": "gpt-default",
                "displayName": "Default",
                "isDefault": True,
            },
            {
                "model": "gpt-other",
                "displayName": "Other",
                "isDefault": False,
            },
        ]

    async def set_agent_model(
        self, user_id: int, name: str, model: str
    ) -> str:
        self.selected.append((user_id, name, model))
        self.state.update_agent(user_id, name, model=model)
        return model


class _FakeResetService(_FakeService):
    def __init__(self) -> None:
        self.consumed: list[tuple[str, str | None]] = []

    async def get_reset_credits(self, account: str) -> dict[str, Any]:
        self.requested_account = account
        return {
            "available_count": 1,
            "credits": [
                {
                    "id": "credit-secret-id",
                    "title": "Weekly reset",
                    "description": "Reset Codex limits",
                    "status": "available",
                    "expiresAt": None,
                }
            ],
            "rate_limits": {},
        }

    async def consume_reset_credit(
        self, account: str, credit_id: str | None
    ) -> str:
        self.consumed.append((account, credit_id))
        return "reset"


class _FakeTurnService(_FakeService):
    async def run_turn(self, user_id: int, name: str, text: str) -> Any:
        self.turn = (user_id, name, text)
        return SimpleNamespace(
            status="completed",
            text="# Summary\n\n**Rendered** response",
            error=None,
        )


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


class BotCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_response_uses_rich_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeTurnService()
            telegram = _RecordingTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]

            await bot._handle_update(  # noqa: SLF001
                {
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "do work",
                    }
                }
            )

            self.assertEqual(service.turn, (42, "main", "do work"))
            self.assertEqual(
                [method for method, _ in telegram.calls],
                ["sendMessage", "sendChatAction", "sendRichMessage"],
            )
            rich_markdown = telegram.calls[-1][1]["rich_message"]["markdown"]
            self.assertIn("## main", rich_markdown)
            self.assertIn("**Rendered**", rich_markdown)

    async def test_agent_button_switches_and_refreshes_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            state.create_agent(42, "reviewer", account="default")
            state.set_active(42, "main")
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            telegram = _RecordingTelegramAPI()
            service = _FakeService()
            bot = TelegramCodexBot(
                config, state, service, telegram  # type: ignore[arg-type]
            )

            await bot._handle_update(  # noqa: SLF001
                {
                    "callback_query": {
                        "id": "query-1",
                        "from": {"id": 42},
                        "message": {
                            "message_id": 7,
                            "chat": {"id": 42, "type": "private"},
                        },
                        "data": "agent:reviewer",
                    }
                }
            )

            self.assertEqual(state.get_active_name(42), "reviewer")
            methods = [method for method, _ in telegram.calls]
            self.assertEqual(methods, ["editMessageText", "answerCallbackQuery"])
            edit_payload = telegram.calls[0][1]
            self.assertIn("当前：reviewer", edit_payload["text"])
            self.assertIn("5 小时剩余 80%", edit_payload["text"])
            self.assertRegex(
                edit_payload["text"],
                r"5 小时剩余 80%（重置 \d{2}-\d{2} \d{2}:\d{2}）",
            )
            self.assertIn("7 天剩余 45%", edit_payload["text"])
            self.assertIn("reply_markup", edit_payload)
            self.assertEqual(service.usage_requests, ["default"])

    async def test_model_button_switches_current_agent_and_refreshes_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeModelService(state)
            telegram = _RecordingTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]

            await bot._handle_update(  # noqa: SLF001
                {
                    "callback_query": {
                        "id": "query-2",
                        "from": {"id": 42},
                        "message": {
                            "message_id": 8,
                            "chat": {"id": 42, "type": "private"},
                        },
                        "data": "model:main:gpt-other",
                    }
                }
            )

            self.assertEqual(service.selected, [(42, "main", "gpt-other")])
            self.assertEqual(state.get_agent(42, "main")["model"], "gpt-other")
            methods = [method for method, _ in telegram.calls]
            self.assertEqual(methods, ["editMessageText", "answerCallbackQuery"])
            self.assertIn("当前模型：Other（gpt-other）", telegram.calls[0][1]["text"])

    async def test_reset_card_requires_selection_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeResetService()
            telegram = _RecordingTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]

            await bot._handle_update(  # noqa: SLF001
                {
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "/reset",
                    }
                }
            )
            reset_message = telegram.calls[-1][1]
            button = reset_message["reply_markup"]["inline_keyboard"][0][0]
            self.assertTrue(button["callback_data"].startswith("resetpick:"))
            self.assertNotIn("credit-secret-id", str(reset_message))
            self.assertEqual(service.consumed, [])

            token = button["callback_data"].split(":", 1)[1]
            callback_base = {
                "from": {"id": 42},
                "message": {
                    "message_id": 9,
                    "chat": {"id": 42, "type": "private"},
                },
            }
            await bot._handle_update(  # noqa: SLF001
                {
                    "callback_query": {
                        **callback_base,
                        "id": "query-pick",
                        "data": f"resetpick:{token}",
                    }
                }
            )
            self.assertEqual(service.consumed, [])
            self.assertIn("确认使用", telegram.calls[-2][1]["text"])

            await bot._handle_update(  # noqa: SLF001
                {
                    "callback_query": {
                        **callback_base,
                        "id": "query-use",
                        "data": f"resetuse:{token}",
                    }
                }
            )
            self.assertEqual(
                service.consumed, [("default", "credit-secret-id")]
            )
