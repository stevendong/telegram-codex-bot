from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_codex_bot.agent_service import AgentService
from telegram_codex_bot.app_server import AppServerError
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
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                            {"reasoningEffort": "high"},
                        ],
                    },
                    {
                        "id": "gpt-fast",
                        "model": "gpt-fast",
                        "displayName": "GPT Fast",
                        "isDefault": False,
                        "defaultReasoningEffort": "low",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                        ],
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
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        raise AssertionError(f"Unexpected request: {method}")


class _ActiveWriterApp(_FakeApp):
    async def request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/resume":
            raise AppServerError(
                f"thread {params['threadId']} already has an active writer "
                "(code=-32600)"
            )
        if method == "thread/fork":
            return {"thread": {"id": "thr-forked"}}
        return await super().request(method, params)


class _InteractiveApp(_FakeApp):
    def __init__(self) -> None:
        super().__init__()
        self.handler: Any = None

    def add_notification_handler(self, handler: Any) -> None:
        self.handler = handler

    def emit(self, method: str, params: dict[str, Any]) -> None:
        self.handler(method, params)

    async def request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thr-run"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-run"}}
        if method == "turn/interrupt":
            self.emit(
                "turn/completed",
                {
                    "threadId": "thr-run",
                    "turn": {"id": "turn-run", "status": "interrupted"},
                },
            )
            return {}
        raise AssertionError(f"Unexpected request: {method}")


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_progress_and_interrupts_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
                codex_cwd=Path(temp),
                codex_sandbox="danger-full-access",
                turn_timeout_seconds=30,
            )
            app = _InteractiveApp()
            service = AgentService(config, state, {"default": app})

            task = asyncio.create_task(service.run_turn(42, "main", "work"))
            for _ in range(100):
                if state.get_agent(42, "main").get("active_turn_id"):
                    break
                await asyncio.sleep(0)
            else:
                self.fail("turn did not start")

            app.emit(
                "turn/plan/updated",
                {
                    "turnId": "turn-run",
                    "plan": [
                        {"step": "Inspect files", "status": "completed"},
                        {"step": "Run tests", "status": "inProgress"},
                    ],
                },
            )
            app.emit(
                "item/started",
                {
                    "threadId": "thr-run",
                    "item": {
                        "type": "commandExecution",
                        "cwd": "/data/project",
                    },
                },
            )
            app.emit(
                "item/completed",
                {
                    "threadId": "thr-run",
                    "item": {"type": "commandExecution"},
                },
            )
            app.emit(
                "item/completed",
                {
                    "threadId": "thr-run",
                    "item": {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "I checked the files and am running tests.",
                    },
                },
            )

            progress = service.get_turn_progress(42, "main")
            self.assertIsNotNone(progress)
            self.assertEqual(progress["plan_completed"], 1)
            self.assertEqual(progress["plan_total"], 2)
            self.assertEqual(progress["completed_items"], 2)
            self.assertEqual(progress["stage"], "Codex 进度更新")
            self.assertIn("running tests", progress["detail"])

            stopped = await service.stop_agent(42, "main")
            stopping = service.get_turn_progress(42, "main")
            result = await asyncio.wait_for(task, timeout=1)

            self.assertTrue(stopped)
            self.assertEqual(stopping["stage"], "正在中断任务")
            self.assertEqual(result.status, "interrupted")
            self.assertNotIn("running tests", result.text)
            self.assertIsNone(service.get_turn_progress(42, "main"))
            turn_params = next(
                params
                for method, params in app.requests
                if method == "turn/start"
            )
            self.assertIn("effort", turn_params)
            self.assertIsNone(turn_params["effort"])
            self.assertEqual(app.requests[-1][0], "turn/interrupt")

    async def test_switch_agent_thread_resumes_selected_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            state.update_agent(
                42, "main", thread_id="thr-old", model="gpt-test"
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

            changed = await service.switch_agent_thread(
                42, "main", "default", "thr-history"
            )

            self.assertEqual(changed, "resumed")
            self.assertEqual(
                state.get_agent(42, "main")["thread_id"], "thr-history"
            )
            self.assertNotIn(
                ("default", "thr-old"), service._loaded_threads  # noqa: SLF001
            )
            self.assertIn(
                ("default", "thr-history"),
                service._loaded_threads,  # noqa: SLF001
            )
            method, params = app.requests[-1]
            self.assertEqual(method, "thread/resume")
            self.assertEqual(params["threadId"], "thr-history")
            self.assertEqual(params["model"], "gpt-test")
            self.assertEqual(params["sandbox"], "danger-full-access")

    async def test_switch_agent_thread_forks_an_active_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.json")
            state.ensure_default(42)
            state.update_agent(42, "main", thread_id="thr-old")
            config = SimpleNamespace(
                max_parallel_turns=4,
                default_account="default",
                codex_model=None,
                codex_cwd=Path(temp),
                codex_sandbox="danger-full-access",
            )
            app = _ActiveWriterApp()
            service = AgentService(config, state, {"default": app})

            outcome = await service.switch_agent_thread(
                42, "main", "default", "thr-active"
            )

            self.assertEqual(outcome, "forked")
            self.assertEqual(
                state.get_agent(42, "main")["thread_id"], "thr-forked"
            )
            self.assertEqual(
                [method for method, _ in app.requests],
                ["thread/resume", "thread/fork"],
            )
            self.assertIn(
                ("default", "thr-forked"),
                service._loaded_threads,  # noqa: SLF001
            )

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

            effort = await service.set_agent_effort(42, "main", "HIGH")
            self.assertEqual(effort, "high")
            self.assertEqual(state.get_agent(42, "main")["effort"], "high")

            selected = await service.set_agent_model(42, "main", "gpt-fast")
            self.assertEqual(selected, "gpt-fast")
            self.assertIsNone(state.get_agent(42, "main")["effort"])

            effort = await service.set_agent_effort(42, "main", "low")
            self.assertEqual(effort, "low")
            effort = await service.set_agent_effort(42, "main", "default")
            self.assertIsNone(effort)
            self.assertIsNone(state.get_agent(42, "main")["effort"])

            with self.assertRaisesRegex(ValueError, "不支持 Effort"):
                await service.set_agent_effort(42, "main", "high")

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
                effort="high",
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
            self.assertEqual(agent["effort"], "high")
            self.assertEqual(agent["account"], "default")
            self.assertIsNone(agent["last_error"])
            self.assertNotIn(
                ("default", "thr-old"), service._loaded_threads  # noqa: SLF001
            )
            self.assertIn(
                ("default", "thr-new"), service._loaded_threads  # noqa: SLF001
            )
            self.assertEqual(app.requests[-1][0], "thread/start")
