from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_codex_bot.agent_service import AgentService
from telegram_codex_bot.state import StateStore


class _FakeApp:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def add_notification_handler(self, handler: Any) -> None:
        del handler

    async def request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "gpt-test",
                        "model": "gpt-test",
                        "displayName": "GPT Test",
                        "isDefault": True,
                    }
                ],
                "nextCursor": None,
            }
        if method == "account/read":
            return {
                "account": {
                    "email": "Example.User@example.com",
                    "type": "chatgpt",
                    "planType": "plus",
                }
            }
        if method == "account/rateLimits/read":
            return {
                "rateLimits": {"primary": {"usedPercent": 80}},
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [
                        {
                            "id": "credit-1",
                            "status": "available",
                            "resetType": "codexRateLimits",
                            "grantedAt": 1,
                        }
                    ],
                },
            }
        if method == "account/rateLimitResetCredit/consume":
            return {"outcome": "reset"}
        if method == "thread/start":
            return {"thread": {"id": "thr-new"}}
        raise AssertionError(f"Unexpected request: {method}")


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_and_clears_agent_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            app = _FakeApp()
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
            )
            service = AgentService(config, state, {"default": app})

            selected = await service.set_agent_model(42, "main", "GPT-TEST")
            self.assertEqual(selected, "gpt-test")
            self.assertEqual(state.get_agent(42, "main")["model"], "gpt-test")
            self.assertEqual(app.requests[0][0], "model/list")

            selected = await service.set_agent_model(42, "main", "default")
            self.assertEqual(selected, "gpt-test")
            self.assertEqual(state.get_agent(42, "main")["model"], "gpt-test")

    async def test_rejects_unavailable_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
            )
            service = AgentService(config, state, {"default": _FakeApp()})

            with self.assertRaisesRegex(ValueError, "不支持模型"):
                await service.set_agent_model(42, "main", "does-not-exist")

    async def test_loads_email_alias_and_consumes_selected_reset_credit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            app = _FakeApp()
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
            )
            service = AgentService(config, state, {"default": app})

            aliases = await service.load_account_aliases()
            self.assertEqual(aliases, {"default": "example.us"})
            snapshot = await service.get_reset_credits("default")
            self.assertEqual(snapshot["available_count"], 1)
            outcome = await service.consume_reset_credit("default", "credit-1")
            self.assertEqual(outcome, "reset")
            method, params = app.requests[-1]
            self.assertEqual(method, "account/rateLimitResetCredit/consume")
            self.assertEqual(params["creditId"], "credit-1")
            self.assertTrue(params["idempotencyKey"])

    async def test_reads_account_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            app = _FakeApp()
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
            )
            service = AgentService(config, state, {"default": app})

            usage = await service.get_account_usage("default")
            self.assertEqual(usage["primary"]["usedPercent"], 80)
            self.assertEqual(app.requests[-1][0], "account/rateLimits/read")

    async def test_clear_agent_context_detaches_thread_and_preserves_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            state.update_agent(
                42,
                "main",
                thread_id="thr-old",
                model="gpt-test",
                last_error="old error",
            )
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
                codex_cwd=Path(temp),
                codex_sandbox="danger-full-access",
            )
            app = _FakeApp()
            service = AgentService(config, state, {"default": app})
            service._loaded_threads.add(("default", "thr-old"))  # noqa: SLF001

            had_context = await service.clear_agent_context(42, "main")

            self.assertTrue(had_context)
            agent = state.get_agent(42, "main")
            self.assertEqual(agent["thread_id"], "thr-new")
            self.assertEqual(agent["model"], "gpt-test")
            self.assertEqual(agent["account"], "default")
            self.assertIsNone(agent["last_error"])
            self.assertNotIn(
                ("default", "thr-old"), service._loaded_threads  # noqa: SLF001
            )
            self.assertIn(
                ("default", "thr-new"), service._loaded_threads  # noqa: SLF001
            )
            self.assertEqual(app.requests[-1][0], "thread/start")
