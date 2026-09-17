from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    """Small atomic JSON store for Telegram aliases and Codex thread IDs."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "telegram_offset": 0, "users": {}}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded.get("version") != 1 or not isinstance(loaded.get("users"), dict):
                raise ValueError(f"Unsupported state file: {self.path}")
            self._data = loaded
            self._data.setdefault("telegram_offset", 0)
            for user in self._data["users"].values():
                for agent in user.get("agents", {}).values():
                    agent.setdefault("account", "default")
                    agent.setdefault("model", None)
                    if agent.get("status") == "running":
                        agent["status"] = "idle"
                        agent["active_turn_id"] = None

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _user_locked(self, user_id: int) -> dict[str, Any]:
        users = self._data["users"]
        return users.setdefault(str(user_id), {"active_agent": None, "agents": {}})

    def ensure_default(self, user_id: int, default_account: str = "default") -> None:
        with self._lock:
            user = self._user_locked(user_id)
            if not user["agents"]:
                stamp = now_iso()
                user["agents"]["main"] = {
                    "account": default_account,
                    "model": None,
                    "thread_id": None,
                    "status": "idle",
                    "active_turn_id": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "last_error": None,
                }
                user["active_agent"] = "main"
                self._save_locked()

    def ensure_account_agents(
        self,
        user_id: int,
        account_names: list[str],
        default_account: str,
    ) -> None:
        """Ensure one immediately switchable Telegram agent per Codex account."""
        with self._lock:
            user = self._user_locked(user_id)
            changed = False
            if not user["agents"]:
                stamp = now_iso()
                user["agents"]["main"] = {
                    "account": default_account,
                    "model": None,
                    "thread_id": None,
                    "status": "idle",
                    "active_turn_id": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "last_error": None,
                }
                user["active_agent"] = "main"
                changed = True
            main = user["agents"].get("main")
            if main is not None and "account" not in main:
                main["account"] = default_account
                changed = True
            represented = {
                agent.get("account", default_account)
                for agent in user["agents"].values()
            }
            stamp = now_iso()
            for account in account_names:
                if account in represented:
                    continue
                alias = account
                suffix = 2
                while alias in user["agents"]:
                    alias = f"{account}-{suffix}"
                    suffix += 1
                user["agents"][alias] = {
                    "account": account,
                    "model": None,
                    "thread_id": None,
                    "status": "idle",
                    "active_turn_id": None,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "last_error": None,
                }
                changed = True
            if changed:
                self._save_locked()

    def create_agent(
        self,
        user_id: int,
        name: str,
        thread_id: str | None = None,
        *,
        account: str = "default",
    ) -> None:
        with self._lock:
            user = self._user_locked(user_id)
            if name in user["agents"]:
                raise ValueError(f"Agent already exists: {name}")
            stamp = now_iso()
            user["agents"][name] = {
                "account": account,
                "model": None,
                "thread_id": thread_id,
                "status": "idle",
                "active_turn_id": None,
                "created_at": stamp,
                "updated_at": stamp,
                "last_error": None,
            }
            user["active_agent"] = name
            self._save_locked()

    def list_agents(self, user_id: int) -> tuple[str | None, dict[str, dict[str, Any]]]:
        with self._lock:
            user = self._user_locked(user_id)
            return user["active_agent"], copy.deepcopy(user["agents"])

    def get_active_name(self, user_id: int) -> str:
        self.ensure_default(user_id)
        with self._lock:
            return str(self._user_locked(user_id)["active_agent"])

    def get_agent(self, user_id: int, name: str) -> dict[str, Any] | None:
        with self._lock:
            agent = self._user_locked(user_id)["agents"].get(name)
            return copy.deepcopy(agent) if agent else None

    def set_active(self, user_id: int, name: str) -> None:
        with self._lock:
            user = self._user_locked(user_id)
            if name not in user["agents"]:
                raise KeyError(name)
            user["active_agent"] = name
            self._save_locked()

    def update_agent(self, user_id: int, name: str, **changes: Any) -> None:
        with self._lock:
            agent = self._user_locked(user_id)["agents"].get(name)
            if agent is None:
                raise KeyError(name)
            agent.update(changes)
            agent["updated_at"] = now_iso()
            self._save_locked()

    def rename_agent(self, user_id: int, old: str, new: str) -> None:
        with self._lock:
            user = self._user_locked(user_id)
            if old not in user["agents"]:
                raise KeyError(old)
            if new in user["agents"]:
                raise ValueError(f"Agent already exists: {new}")
            user["agents"][new] = user["agents"].pop(old)
            if user["active_agent"] == old:
                user["active_agent"] = new
            self._save_locked()

    def detach_agent(self, user_id: int, name: str) -> dict[str, Any]:
        with self._lock:
            user = self._user_locked(user_id)
            if name not in user["agents"]:
                raise KeyError(name)
            removed = user["agents"].pop(name)
            if user["active_agent"] == name:
                user["active_agent"] = next(iter(user["agents"]), None)
            self._save_locked()
            return copy.deepcopy(removed)

    def get_telegram_offset(self) -> int:
        with self._lock:
            return int(self._data.get("telegram_offset", 0))

    def set_telegram_offset(self, offset: int) -> None:
        with self._lock:
            self._data["telegram_offset"] = offset
            self._save_locked()
