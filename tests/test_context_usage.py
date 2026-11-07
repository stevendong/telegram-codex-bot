from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from telegram_codex_bot.agent_service import AgentService
from telegram_codex_bot.app_server import AppServerError
from telegram_codex_bot.bot import format_status
from telegram_codex_bot.context_usage import normalize_context_usage, read_context_usage
from telegram_codex_bot.state import StateStore


def token_event(used: int, window: int | None = 475000) -> str:
    return json.dumps({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": used},
            "total_token_usage": {"total_tokens": used * 10},
            "model_context_window": window,
        }},
    }) + "\n"


class ContextUsageTests(unittest.TestCase):
    def test_normalizes_notification_and_rejects_invalid_usage(self) -> None:
        self.assertEqual(normalize_context_usage({
            "last": {"totalTokens": 46068},
            "total": {"totalTokens": 219729},
            "modelContextWindow": 475000,
        }), {"used_tokens": 46068, "window_tokens": 475000})
        for last in (None, [], "bad", {"totalTokens": -1}, {"totalTokens": True}):
            with self.subTest(last=last):
                self.assertIsNone(normalize_context_usage({"last": last}))
        for window in (None, 0, -1, True, "475000"):
            with self.subTest(window=window):
                self.assertEqual(normalize_context_usage({
                    "last": {"totalTokens": 0}, "modelContextWindow": window,
                }), {"used_tokens": 0, "window_tokens": None})

    def test_rollout_tail_reads_latest_usage_across_blocks_and_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text(
                token_event(100000) + token_event(46068)
                + json.dumps({"type": "event_msg", "payload": "x" * 150000}) + "\n"
                + '{"type":"event_msg","payload":{"type":"token_count","info":null}}\n'
                + '{"type":"event_msg","payload":{"type":"token_count",',
            )
            self.assertEqual(read_context_usage(path), {
                "used_tokens": 46068, "window_tokens": 475000,
            })
            path.write_text(token_event(0).rstrip("\n"))
            self.assertEqual(read_context_usage(path)["used_tokens"], 0)
            path.write_text("[]\n{}\n")
            self.assertIsNone(read_context_usage(path))

    def test_format_context_and_unavailable_states(self) -> None:
        agent = {"thread_id": "thr-1"}
        for used, window, expected in (
            (46068, 475000, "46,068 / 475,000 tokens（剩余 90.3%）"),
            (0, 475000, "0 / 475,000 tokens（剩余 100.0%）"),
            (500000, 475000, "500,000 / 475,000 tokens（剩余 0.0%）"),
            (12345, None, "已用 12,345 tokens（窗口上限未知）"),
        ):
            with self.subTest(used=used, window=window):
                self.assertIn("Context：" + expected, format_status(
                    "main", agent, {}, {"used_tokens": used, "window_tokens": window},
                ))
        self.assertIn("Context：暂无用量数据", format_status("main", agent, {}))
        self.assertIn("Context：尚未创建会话", format_status("main", {}, {}))


class ContextServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.state.ensure_default(42)
        self.app = SimpleNamespace(
            request=AsyncMock(), add_notification_handler=lambda handler: None,
        )
        self.service = AgentService(
            SimpleNamespace(max_parallel_turns=4, default_account="default"),
            self.state, {"default": self.app, "other": self.app},
        )

    async def test_live_usage_is_keyed_by_account_and_thread_even_without_active_run(self) -> None:
        for account, thread, used in (
            ("default", "thr-1", 100), ("other", "thr-1", 200),
            ("default", "thr-2", 300),
        ):
            self.service._on_notification(account, "thread/tokenUsage/updated", {
                "threadId": thread, "tokenUsage": {
                    "last": {"totalTokens": used}, "modelContextWindow": 1000,
                },
            })
            self.state.update_agent(42, "main", account=account, thread_id=thread)
            self.assertEqual(await self.service.get_agent_context(42, "main"), {
                "used_tokens": used, "window_tokens": 1000,
            })
        self.app.request.assert_not_awaited()
        self.state.update_agent(42, "main", thread_id=None)
        self.assertIsNone(await self.service.get_agent_context(42, "main"))

    async def test_restores_history_read_only_and_reads_new_disk_stats(self) -> None:
        path = Path(self.temp.name) / "rollout.jsonl"
        path.write_text(token_event(46068))
        self.app.request.return_value = {"thread": {"path": str(path)}}
        self.state.update_agent(42, "main", thread_id="imported")
        self.assertEqual(await self.service.get_agent_context(42, "main"), {
            "used_tokens": 46068, "window_tokens": 475000,
        })
        self.app.request.assert_awaited_once_with(
            "thread/read", {"threadId": "imported", "includeTurns": False},
        )
        with path.open("a") as handle:
            handle.write(token_event(1000))
        self.assertEqual((await self.service.get_agent_context(42, "main"))["used_tokens"], 1000)

    async def test_unavailable_history_does_not_break_status(self) -> None:
        self.state.update_agent(42, "main", thread_id="missing")
        for response in ({"thread": {"path": None}}, {"thread": {"path": "/missing/context.jsonl"}}):
            self.app.request.return_value = response
            if response["thread"]["path"]:
                with self.assertLogs("telegram_codex_bot.agent_service", "WARNING"):
                    self.assertIsNone(await self.service.get_agent_context(42, "main"))
            else:
                self.assertIsNone(await self.service.get_agent_context(42, "main"))
        self.app.request.side_effect = AppServerError("thread unavailable")
        with self.assertLogs("telegram_codex_bot.agent_service", "WARNING"):
            self.assertIsNone(await self.service.get_agent_context(42, "main"))

    async def test_live_notification_wins_when_arriving_during_history_lookup(self) -> None:
        self.state.update_agent(42, "main", thread_id="thr-1")

        async def read_thread(*args: Any) -> dict[str, Any]:
            self.service._on_notification("default", "thread/tokenUsage/updated", {
                "threadId": "thr-1", "tokenUsage": {"last": {"totalTokens": 99}},
            })
            return {"thread": {"path": None}}

        self.app.request.side_effect = read_thread
        self.assertEqual(await self.service.get_agent_context(42, "main"), {
            "used_tokens": 99, "window_tokens": None,
        })
