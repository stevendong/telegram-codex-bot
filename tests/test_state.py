from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from telegram_codex_bot.state import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = StateStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_agent_and_persistence(self) -> None:
        self.state.ensure_default(42)
        self.assertEqual(self.state.get_active_name(42), "main")
        self.state.update_agent(42, "main", thread_id="thr_1")

        reloaded = StateStore(self.path)
        self.assertEqual(reloaded.get_agent(42, "main")["thread_id"], "thr_1")
        self.assertIsNone(reloaded.get_agent(42, "main")["model"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_switch_rename_and_detach(self) -> None:
        self.state.ensure_default(42)
        self.state.create_agent(42, "reviewer", "thr_2")
        self.assertEqual(self.state.get_active_name(42), "reviewer")
        self.state.rename_agent(42, "reviewer", "audit")
        self.assertEqual(self.state.get_active_name(42), "audit")
        removed = self.state.detach_agent(42, "audit")
        self.assertEqual(removed["thread_id"], "thr_2")

    def test_running_state_is_recovered_as_idle(self) -> None:
        self.state.ensure_default(42)
        self.state.update_agent(42, "main", status="running", active_turn_id="turn_1")
        reloaded = StateStore(self.path)
        agent = reloaded.get_agent(42, "main")
        self.assertEqual(agent["status"], "idle")
        self.assertIsNone(agent["active_turn_id"])

    def test_file_is_valid_json(self) -> None:
        self.state.ensure_default(42)
        with self.path.open(encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["version"], 1)

    def test_ensures_one_agent_per_account(self) -> None:
        self.state.ensure_account_agents(
            42,
            {
                "default": "silviafach",
                "account4": "kyaaliya13",
                "agentopt": "josephcunn",
            },
            "default",
        )
        active, agents = self.state.list_agents(42)
        self.assertEqual(active, "silviafach")
        self.assertEqual(
            {name: agent["account"] for name, agent in agents.items()},
            {
                "silviafach": "default",
                "kyaaliya13": "account4",
                "josephcunn": "agentopt",
            },
        )

    def test_migrates_primary_names_without_losing_thread(self) -> None:
        self.state.ensure_default(42)
        self.state.update_agent(42, "main", thread_id="thr_kept", model="gpt-test")
        self.state.ensure_account_agents(
            42, {"default": "silviafach"}, "default"
        )
        self.assertIsNone(self.state.get_agent(42, "main"))
        migrated = self.state.get_agent(42, "silviafach")
        self.assertEqual(migrated["thread_id"], "thr_kept")
        self.assertEqual(migrated["model"], "gpt-test")
        self.assertTrue(migrated["account_primary"])
        self.assertEqual(self.state.get_active_name(42), "silviafach")
