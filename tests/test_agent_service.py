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
