from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_codex_bot.config import Config, _parse_accounts


class ConfigTests(unittest.TestCase):
    def test_parse_multiple_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            accounts = _parse_accounts(f"one={first},two={second}")
            self.assertEqual(accounts, {"one": first, "two": second})

    def test_rejects_empty_user_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codex = Path(temp) / "codex"
            codex.touch()
            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_USER_IDS": "",
                "CODEX_BIN": str(codex),
                "CODEX_CWD": temp,
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "must not be empty"):
                    Config.from_env()
