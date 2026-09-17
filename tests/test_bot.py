from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from telegram_codex_bot.bot import TelegramCodexBot
from telegram_codex_bot.state import StateStore
from telegram_codex_bot.telegram_api import TelegramAPI


class _FakeService:
    def __init__(self) -> None:
        self.usage_requests: list[str] = []
        self.cleared: list[tuple[int, str]] = []

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

    async def clear_agent_context(self, user_id: int, name: str) -> bool:
        self.cleared.append((user_id, name))
        return True


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


class _FakeThreadService(_FakeService):
    def __init__(self, state: StateStore) -> None:
        super().__init__()
        self.state = state
        self.switched: list[tuple[int, str, str, str]] = []
        self.switch_outcome = "resumed"

    async def list_server_threads(
        self, account: str, limit: int = 15
    ) -> list[dict[str, Any]]:
        del account, limit
        return [
            {
                "id": "thr-current",
                "name": "Current session",
                "status": {"type": "idle"},
            },
            {
                "id": "thr-history",
                "name": "Historical build session",
                "status": {"type": "idle"},
            },
        ]

    async def switch_agent_thread(
        self,
        user_id: int,
        name: str,
        account: str,
        thread_id: str,
    ) -> str:
        self.switched.append((user_id, name, account, thread_id))
        self.state.update_agent(user_id, name, thread_id=thread_id)
        return self.switch_outcome


class _FakeProgressService(_FakeService):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stop_requests: list[tuple[int, str]] = []

    async def run_turn(self, user_id: int, name: str, text: str) -> Any:
        self.turn = (user_id, name, text)
        self.started.set()
        await self.finished.wait()
        return SimpleNamespace(
            status="interrupted",
            text="任务已中止。",
            error=None,
        )

    def get_turn_progress(self, user_id: int, name: str) -> dict[str, Any]:
        del user_id, name
        return {
            "stage": "正在执行命令",
            "detail": "/data/project",
            "completed_items": 2,
            "plan_completed": 1,
            "plan_total": 3,
            "elapsed_seconds": 12,
        }

    async def stop_agent(self, user_id: int, name: str) -> bool:
        self.stop_requests.append((user_id, name))
        self.finished.set()
        return True


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


class _ProgressTelegramAPI(_RecordingTelegramAPI):
    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 70,
    ) -> Any:
        await super().call(method, payload, timeout=timeout)
        if method == "sendMessage":
            return {"message_id": 99}
        return True


class BotCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_card_updates_and_interrupt_button_stops_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeProgressService()
            telegram = _ProgressTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]
            prompt_update = {
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": "long task",
                }
            }

            with patch(
                "telegram_codex_bot.bot.PROGRESS_UPDATE_SECONDS", 0.01
            ):
                prompt_task = asyncio.create_task(
                    bot._handle_update(prompt_update)  # noqa: SLF001
                )
                await asyncio.wait_for(service.started.wait(), timeout=1)
                await asyncio.sleep(0.03)

                initial = next(
                    payload
                    for method, payload in telegram.calls
                    if method == "sendMessage"
                )
                stop_button = initial["reply_markup"]["inline_keyboard"][0][0]
                self.assertEqual(stop_button["callback_data"], "stop:main")
                progress_edits = [
                    payload
                    for method, payload in telegram.calls
                    if method == "editMessageText"
                    and "计划进度：1/3" in payload.get("text", "")
                ]
                self.assertTrue(progress_edits)

                await bot._handle_update(  # noqa: SLF001
                    {
                        "callback_query": {
                            "id": "query-stop",
                            "from": {"id": 42},
                            "message": {
                                "message_id": 99,
                                "text": progress_edits[-1]["text"],
                                "chat": {"id": 42, "type": "private"},
                            },
                            "data": stop_button["callback_data"],
                        }
                    }
                )
                await asyncio.wait_for(prompt_task, timeout=1)

            self.assertEqual(service.stop_requests, [(42, "main")])
            self.assertTrue(
                any(
                    method == "answerCallbackQuery"
                    and "已请求中断" in payload.get("text", "")
                    for method, payload in telegram.calls
                )
            )
            self.assertTrue(
                any(
                    method == "editMessageText"
                    and "任务已中止" in payload.get("text", "")
                    for method, payload in telegram.calls
                )
            )

    async def test_threads_command_switches_history_with_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            state.update_agent(42, "main", thread_id="thr-current")
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeThreadService(state)
            telegram = _RecordingTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]

            await bot._handle_update(  # noqa: SLF001
                {
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "/threads",
                    }
                }
            )

            picker = telegram.calls[-1][1]
            self.assertIn("点击按钮即可切换", picker["text"])
            buttons = picker["reply_markup"]["inline_keyboard"]
            self.assertEqual(buttons[0][0]["callback_data"], "noop")
            callback_data = buttons[1][0]["callback_data"]
            self.assertTrue(callback_data.startswith("thread:"))
            self.assertLessEqual(len(callback_data.encode()), 64)
            service.switch_outcome = "forked"

            await bot._handle_update(  # noqa: SLF001
                {
                    "callback_query": {
                        "id": "query-thread",
                        "from": {"id": 42},
                        "message": {
                            "message_id": 10,
                            "chat": {"id": 42, "type": "private"},
                        },
                        "data": callback_data,
                    }
                }
            )

            self.assertEqual(
                service.switched,
                [(42, "main", "default", "thr-history")],
            )
            self.assertEqual(state.get_agent(42, "main")["thread_id"], "thr-history")
            self.assertEqual(
                [method for method, _ in telegram.calls[-2:]],
                ["editMessageText", "answerCallbackQuery"],
            )
            self.assertIn("已分叉上下文", telegram.calls[-1][1]["text"])

    async def test_clear_command_starts_fresh_context_for_current_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                allowed_user_ids=frozenset({42}),
                default_account="default",
                codex_model=None,
            )
            service = _FakeService()
            telegram = _RecordingTelegramAPI()
            bot = TelegramCodexBot(config, state, service, telegram)  # type: ignore[arg-type]

            await bot._handle_update(  # noqa: SLF001
                {
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "/clear",
                    }
                }
            )

            self.assertEqual(service.cleared, [(42, "main")])
            self.assertEqual(telegram.calls[-1][0], "sendMessage")
            self.assertIn("已启动全新会话", telegram.calls[-1][1]["text"])

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
