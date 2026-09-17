from __future__ import annotations

import unittest

from telegram_codex_bot.agent_service import normalize_agent_name
from telegram_codex_bot.bot import format_models, format_status, parse_command
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

    def test_format_status_includes_current_account_quota(self) -> None:
        text = format_status(
            "main",
            {"account": "default", "status": "failed", "thread_id": "thr_1"},
            {
                "name": "default",
                "logged_in": True,
                "type": "chatgpt",
                "plan_type": "plus",
                "rate_limits": {
                    "rateLimitReachedType": "rate_limit_reached",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 300,
                        "resetsAt": None,
                    },
                    "secondary": {
                        "usedPercent": 48,
                        "windowDurationMins": 10080,
                        "resetsAt": None,
                    },
                },
                "rate_limit_error": None,
            },
        )
        self.assertIn("Codex 账号：default", text)
        self.assertIn("登录状态：已登录（ChatGPT / plus）", text)
        self.assertIn("额度状态：已达到额度上限", text)
        self.assertIn("5 小时额度：已用 100%，剩余 0%", text)
        self.assertIn("7 天额度：已用 48%，剩余 52%", text)

    def test_format_models_marks_selected_model(self) -> None:
        text, markup = format_models(
            "main",
            "default",
            [
                {
                    "model": "gpt-default",
                    "displayName": "Default",
                    "isDefault": True,
                    "supportedReasoningEfforts": [],
                },
                {
                    "model": "gpt-selected",
                    "displayName": "Selected",
                    "isDefault": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"}
                    ],
                },
            ],
            "gpt-selected",
        )
        self.assertIn("当前模型：Selected（gpt-selected）", text)
        buttons = [
            button
            for row in markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn(
            {
                "text": "Default · 默认",
                "callback_data": "model:main:gpt-default",
            },
            buttons,
        )
        self.assertTrue(
            all(len(button["callback_data"].encode()) <= 64 for button in buttons)
        )
        self.assertIn(
            {"text": "✅ Selected", "callback_data": "noop"},
            buttons,
        )
        self.assertIn(
            {
                "text": "↩️ 使用账号默认模型",
                "callback_data": "model:main:default",
            },
            buttons,
        )
