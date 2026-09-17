from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import __version__

LOG = logging.getLogger(__name__)
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
APP_SERVER_STREAM_LIMIT = 64 * 1024 * 1024


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, codex_bin: str, cwd: Path, codex_home: Path | None = None):
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.codex_home = codex_home
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: list[NotificationHandler] = []
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        # npm/NVM installs use `#!/usr/bin/env node` for the Codex launcher.
        # systemd does not inherit the interactive shell's NVM PATH, so ensure
        # the directory containing `codex` (and normally `node`) is present.
        child_env = os.environ.copy()
        codex_bin_dir = str(Path(self.codex_bin).expanduser().absolute().parent)
        current_path = child_env.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []
        if codex_bin_dir not in path_parts:
            child_env["PATH"] = os.pathsep.join([codex_bin_dir, *path_parts])
        if self.codex_home is not None:
            child_env["CODEX_HOME"] = str(self.codex_home)
        self.process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--listen",
            "stdio://",
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
            env=child_env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram_codex_bot",
                    "title": "Telegram Codex Bot",
                    "version": __version__,
                }
            },
            timeout=30,
        )
        await self.notify("initialized", {})

    async def close(self) -> None:
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )
        self._fail_pending(AppServerError("Codex app-server stopped"))

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60,
    ) -> dict[str, Any]:
        if not self.process or self.process.returncode is not None:
            raise AppServerError("Codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise AppServerError("Codex app-server is not running")
        wire = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self.process.stdin.write(wire)
            await self.process.stdin.drain()

    async def _reader_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("Invalid JSON from app-server: %r", line[:500])
                    continue
                if "id" in message and "method" in message:
                    asyncio.create_task(self._handle_server_request(message))
                elif "id" in message:
                    self._handle_response(message)
                elif "method" in message:
                    self._handle_notification(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Codex app-server reader failed")
        finally:
            self._fail_pending(AppServerError("Codex app-server connection closed"))

    async def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        try:
            while line := await self.process.stderr.readline():
                LOG.info("codex-app-server: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise

    def _handle_response(self, message: dict[str, Any]) -> None:
        future = self._pending.get(message["id"])
        if not future or future.done():
            return
        if "error" in message:
            error = message["error"]
            future.set_exception(
                AppServerError(f"{error.get('message', 'request failed')} (code={error.get('code')})")
            )
        else:
            future.set_result(message.get("result", {}))

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params") or {}
        for handler in self._handlers:
            try:
                result = handler(method, params)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception:
                LOG.exception("Notification handler failed for %s", method)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        request_id = message["id"]
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result: dict[str, Any] = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        else:
            await self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Client does not support server request: {method}",
                    },
                }
            )
            return
        LOG.warning("Declined app-server request %s", method)
        await self._send({"id": request_id, "result": result})

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
