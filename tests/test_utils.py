from __future__ import annotations

import unittest

from telegram_codex_bot.agent_service import normalize_agent_name
from telegram_codex_bot.bot import parse_command
from telegram_codex_bot.telegram_api import split_text


class UtilityTests(unittest.TestCase):
    def test_normalize_agent_name(self) -> None:
        self.assertEqual(normalize_agent_name(" Reviewer-1 "), "reviewer-1")
        with self.assertRaises(ValueError):
            normalize_agent_name("bad agent")

    def test_parse_command(self) -> None:
        self.assertEqual(parse_command("/agent@my_bot reviewer"), ("agent", ["reviewer"]))
        self.assertIsNone(parse_command("hello"))

    def test_split_text_respects_limit(self) -> None:
        chunks = split_text("first line\n" + "x" * 100, limit=40)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), ("first line" + "x" * 100))
