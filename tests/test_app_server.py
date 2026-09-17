from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from telegram_codex_bot.app_server import APP_SERVER_STREAM_LIMIT, CodexAppServer


class _FakeStdin:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data))

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.returncode = None


class AppServerTests(unittest.IsolatedAsyncioTestCase):
    def test_stream_limit_accepts_large_thread_payloads(self) -> None:
        self.assertGreaterEqual(APP_SERVER_STREAM_LIMIT, 16 * 1024 * 1024)

    async def test_approval_requests_are_declined(self) -> None:
        client = CodexAppServer("codex", Path("/data"))
        process = _FakeProcess()
        client.process = process  # type: ignore[assignment]
        await client._handle_server_request(  # noqa: SLF001
            {
                "id": 99,
                "method": "item/commandExecution/requestApproval",
                "params": {},
            }
        )
        self.assertEqual(process.stdin.messages[-1], {"id": 99, "result": {"decision": "decline"}})

    async def test_response_resolves_pending_future(self) -> None:
        client = CodexAppServer("codex", Path("/data"))
        future = asyncio.get_running_loop().create_future()
        client._pending[1] = future  # noqa: SLF001
        client._handle_response({"id": 1, "result": {"ok": True}})  # noqa: SLF001
        self.assertEqual(await future, {"ok": True})
