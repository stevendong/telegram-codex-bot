from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mail_digest.service import (
    AmbiguousDelivery, Classifier, Config, DigestError, MailSource, Store,
    alert_failure, clean_body, control_store, deliver, render, run, split_message, validate_result,
)


def email(number=1, **updates):
    row = dict(id=f"00000000-0000-0000-0000-{number:012d}", direction="inbound",
               from_email="Kris <customer@example.com>", to_email="support@example.com",
               subject="Billing", text_body="Charged $8.41 and received no credits.",
               html_body=None, is_read=False, created_at="2026-09-10T00:00:00.123456")
    return row | updates


def analysis(row, **updates):
    return dict(id=row["id"], priority="P1", category="billing", summary="用户反馈扣款未到账",
                interpretation="这是用户自述，需要核查真实账本。", action="核查付款和积分记录。",
                evidence="Charged $8.41", confidence=0.95, reply_status="no_reply_found") | updates


def config(folder):
    return Config("https://project.supabase.co", "db-secret", "bot-secret", 123,
                  "/bin/codex", Path(folder) / "auth", Path(folder) / "state")


class SourceTests(unittest.TestCase):
    def test_html_hides_script_and_preserves_tail_on_truncation(self):
        body, truncated = clean_body(email(text_body="", html_body="<style>secret</style><p>Hi</p><script>act()</script><p>Refund</p>"))
        self.assertIn("Refund", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("act()", body)
        self.assertFalse(truncated)
        body, truncated = clean_body(email(text_body="a" * 500 + "IMPORTANT END"), 100)
        self.assertTrue(truncated)
        self.assertTrue(body.endswith("IMPORTANT END"))

    def test_keyset_pagination_keeps_same_timestamp_ids(self):
        source = MailSource(config("/tmp"))
        first = [email(n) for n in range(1, 101)]
        with patch.object(source, "get", side_effect=[first, [email(101)]]) as get:
            rows = source.scan("2026-09-09T00:00:00", "2026-09-11T00:00:00")
        self.assertEqual(len(rows), 101)
        second_query = get.call_args_list[1].args[0]
        self.assertIn("id.gt." + email(100)["id"], second_query["or"])
        self.assertEqual(second_query["created_at"], "lt.2026-09-11T00:00:00")
        self.assertIn("2026-09-09", second_query["and"])

    def test_refresh_deleted_email_does_not_remind_forever(self):
        source = MailSource(config("/tmp"))
        with patch.object(source, "get", return_value=[{"id": email(1)["id"], "is_read": False}]):
            flags = source.refresh([email(1)["id"], email(2)["id"]])
        self.assertEqual(flags, {email(1)["id"]: False, email(2)["id"]: True})

    def test_unread_scan_checks_old_mail_without_a_date_lower_bound(self):
        source = MailSource(config("/tmp"))
        with patch.object(source, "get", return_value=[email()]) as get:
            source.scan(None, "2026-10-15T00:00:00+00:00", unread_only=True)
        query = get.call_args.args[0]
        self.assertEqual(query["is_read"], "eq.false")
        self.assertEqual(query["direction"], "eq.inbound")
        self.assertNotIn("and", query)

    def test_incomplete_duplicate_foreign_and_low_confidence_results(self):
        for rows in [[], [analysis(email()), analysis(email())], [analysis(email(2))]]:
            with self.assertRaises(DigestError):
                validate_result({"items": rows}, [email()["id"]])
        item = validate_result({"items": [analysis(email(), priority="P3", confidence=0.4)]}, [email()["id"]])[0]
        self.assertEqual(item["priority"], "P1")
        self.assertIn("人工复核", item["action"])

    def test_nan_confidence_is_rejected(self):
        with self.assertRaises(DigestError):
            validate_result({"items": [analysis(email(), confidence=float("nan"))]}, [email()["id"]])


class ReportingTests(unittest.TestCase):
    def test_marketing_does_not_hide_customer_complaint(self):
        customer = {"source": email(), "analysis": analysis(email()), "reported": False}
        ad = {"source": email(2, subject="SEO links"), "analysis": analysis(email(2), priority="P3", category="marketing"), "reported": False}
        summary, full = render([ad, customer], "2026-10-14", True)
        self.assertLess(summary.index("Billing"), summary.index("营销/低价值汇总"))
        self.assertNotIn("SEO links", summary)
        self.assertIn("SEO links", full)
        self.assertIn("账单/风险/故障均为发送方自述", summary)

    def test_only_unread_important_previously_reported_mail_is_reminded(self):
        rows = [{"source": email(n, is_read=(n == 2)), "analysis": analysis(email(n), priority=("P3" if n == 3 else "P1")), "reported": True} for n in [1, 2, 3]]
        summary, full = render(rows, "2026-10-14", False)
        self.assertIn("此前未读的重要邮件 1 封", summary)
        self.assertIn(email(1)["id"], full)
        self.assertNotIn(email(2)["id"], full)
        self.assertNotIn(email(3)["id"], full)

    def test_emoji_message_parts_fit_telegram_utf16_limit(self):
        text = "📬" * 4000 + "中文" * 1000
        parts = split_message(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 3500 for p in parts))

    def test_unread_important_then_newest_are_shown_before_old_read_mail(self):
        rows = []
        for n, date, read in [(1, '2026-07-01T00:00:00', True),
                              (2, '2026-10-13T00:00:00', True),
                              (3, '2026-09-01T00:00:00', False)]:
            source = email(n, subject=f'case-{n}', created_at=date, is_read=read)
            rows.append({'source':source, 'analysis':analysis(source), 'reported':False})
        summary, _ = render(rows, '2026-10-14', True)
        self.assertLess(summary.index('case-3'), summary.index('case-2'))
        self.assertLess(summary.index('case-2'), summary.index('case-1'))
        self.assertIn('2026-09-01 08:00', summary)


class FakeTelegram:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []
        self.messages = []

    def message(self, text):
        self.calls.append("message")
        self.messages.append(text)
        if len(self.calls) in self.failures:
            raise self.failures[len(self.calls)]
        return len(self.calls)

    def document(self, text, date):
        self.calls.append("document")
        if len(self.calls) in self.failures:
            raise self.failures[len(self.calls)]
        return len(self.calls)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.pacing = patch('mail_digest.service.time.sleep')
        self.pacing.start()
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.store.cache([email()], [analysis(email())])
        self.payload = dict(date="2026-10-14", summary="a" * 4000, full_report="full",
                            scan_until="2026-10-14T00:00:00+00:00", email_ids=[email()["id"]])
        self.store.enqueue(self.payload)

    def tearDown(self):
        self.pacing.stop()
        self.store.db.close()
        self.temp.cleanup()

    def test_partial_definite_failure_retries_only_remaining_parts(self):
        first = FakeTelegram({2: DigestError("telegram_http_429")})
        with self.assertRaises(DigestError):
            deliver(self.store, first)
        self.assertIsNone(self.store.get("scan_until"))
        self.assertFalse(self.store.records()[0]["reported"])
        self.assertEqual(self.store.pending()["next_part"], 1)
        retry = FakeTelegram()
        deliver(self.store, retry)
        self.assertEqual(retry.calls, ["message", "document"])
        self.assertEqual(self.store.get("scan_until"), self.payload["scan_until"])
        self.assertTrue(self.store.records()[0]["reported"])
        self.assertIsNone(self.store.pending())

    def test_ambiguous_send_is_not_automatically_duplicated(self):
        first = FakeTelegram({1: AmbiguousDelivery("telegram_delivery_uncertain")})
        with self.assertRaises(AmbiguousDelivery):
            deliver(self.store, first)
        retry = FakeTelegram()
        with self.assertRaisesRegex(DigestError, "confirmation"):
            deliver(self.store, retry)
        self.assertEqual(retry.calls, [])
        self.assertIsNone(self.store.get("scan_until"))

    def test_crash_after_network_send_before_ack_is_preserved_as_uncertain(self):
        with patch.object(FakeTelegram, "message", side_effect=RuntimeError("process crash")):
            with self.assertRaises(RuntimeError):
                deliver(self.store, FakeTelegram())
        self.assertEqual(self.store.pending()["status"], "uncertain")

    def test_message_only_failure_keeps_checkpoint_then_retries_without_attachment(self):
        payload = self.payload | {"summary": "暂无新增未读邮件", "full_report": None}
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET payload=?", (json.dumps(payload),))
        with self.assertRaises(DigestError):
            deliver(self.store, FakeTelegram({1: DigestError("telegram_http_429")}))
        self.assertIsNone(self.store.get("scan_until"))
        self.assertEqual(self.store.pending()["next_part"], 0)
        retry = FakeTelegram()
        deliver(self.store, retry)
        self.assertEqual(retry.calls, ["message"])
        self.assertEqual(self.store.get("scan_until"), payload["scan_until"])
        self.assertIsNone(self.store.pending())


class RunTests(unittest.TestCase):
    def setUp(self):
        self.pacing = patch('mail_digest.service.time.sleep')
        self.pacing.start()

    def tearDown(self):
        self.pacing.stop()

    def test_dry_run_caches_and_sending_reuses_classifications(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            with patch.object(MailSource, "scan", side_effect=[[email()], [], [email()]]), \
                 patch.object(MailSource, "refresh", return_value={email()["id"]: False}), \
                 patch.object(Classifier, "classify", return_value=[analysis(email())]) as classify, \
                 patch("mail_digest.service.Telegram", return_value=FakeTelegram()) as telegram:
                run(cfg, dry_run=True)
                store = Store(cfg.state_dir / "state.sqlite3")
                self.assertIsNone(store.get("scan_until"))
                store.db.close()
                self.assertFalse(telegram.return_value.calls)
                run(cfg)
                self.assertEqual(classify.call_count, 1)
                self.assertEqual(telegram.return_value.calls, ["message", "document"])
                store = Store(cfg.state_dir / "state.sqlite3")
                self.assertTrue(store.records()[0]["reported"])
                store.db.close()

    def test_classification_failure_does_not_create_outbox_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            with patch.object(MailSource, "scan", side_effect=[[email()], []]), \
                 patch.object(Classifier, "classify", side_effect=DigestError("codex_incomplete_or_duplicate_ids")):
                with self.assertRaises(DigestError):
                    run(cfg)
            store = Store(cfg.state_dir / "state.sqlite3")
            self.assertIsNone(store.get("scan_until"))
            self.assertIsNone(store.pending())
            store.db.close()

    def test_no_unread_mail_sends_only_notification_without_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", return_value=[]) as scan, \
                 patch("mail_digest.service.Classifier") as classifier, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg)
                run(cfg)
            classifier.assert_not_called()
            scan.assert_called_once()
            self.assertEqual(scan.call_args.args[0], None)
            self.assertTrue(scan.call_args.kwargs["unread_only"])
            self.assertEqual(telegram.calls, ["message"])
            self.assertIn("当前未读邮件 0 封", telegram.messages[0])
            self.assertIn("未启动 Agent", telegram.messages[0])
            store = Store(cfg.state_dir / "state.sqlite3")
            self.assertIsNotNone(store.get("scan_until"))
            self.assertIsNotNone(store.get("last_report_date"))
            self.assertIsNone(store.pending())
            store.db.close()

    def test_new_read_mail_does_not_launch_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", return_value=[email(is_read=True)]) as scan, \
                 patch("mail_digest.service.Classifier") as classifier, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg)
            classifier.assert_not_called()
            scan.assert_called_once()
            self.assertEqual(telegram.calls, ["message"])
            store = Store(cfg.state_dir / "state.sqlite3")
            self.assertEqual(store.cached_ids(), set())
            store.db.close()

    def test_old_unread_important_mail_reuses_cache_for_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            cfg.state_dir.mkdir()
            store = Store(cfg.state_dir / "state.sqlite3")
            store.cache([email()], [analysis(email())])
            with store.db:
                store.db.execute("UPDATE emails SET reported=1")
                store.put("scan_until", "2026-10-13T00:00:00+00:00")
            store.db.close()
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", return_value=[email()]), \
                 patch.object(MailSource, "refresh", return_value={email()["id"]: False}), \
                 patch("mail_digest.service.Classifier") as classifier, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg)
            classifier.assert_not_called()
            self.assertEqual(telegram.calls, ["message"])
            self.assertIn("其中重要邮件 1 封", telegram.messages[0])
            self.assertIn("Billing", telegram.messages[0])

    def test_only_unclassified_unread_mail_is_analyzed_even_when_older_than_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            cfg.state_dir.mkdir()
            store = Store(cfg.state_dir / "state.sqlite3")
            with store.db:
                store.put("scan_until", "2026-10-13T00:00:00+00:00")
            store.db.close()
            unread = email()
            read = email(2, is_read=True)
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", side_effect=[[read, unread], []]) as scan, \
                 patch.object(MailSource, "refresh", return_value={unread["id"]: False}), \
                 patch.object(Classifier, "classify", return_value=[analysis(unread)]) as classify, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg)
            classify.assert_called_once_with([unread], [])
            self.assertIsNone(scan.call_args_list[0].args[0])
            self.assertTrue(scan.call_args_list[0].kwargs["unread_only"])
            self.assertEqual(telegram.calls, ["message", "document"])

    def test_empty_dry_run_does_not_send_or_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", return_value=[]), \
                 patch("mail_digest.service.Classifier") as classifier, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg, dry_run=True)
            classifier.assert_not_called()
            self.assertEqual(telegram.calls, [])
            store = Store(cfg.state_dir / "state.sqlite3")
            self.assertIsNone(store.get("scan_until"))
            self.assertIsNone(store.get("last_report_date"))
            store.db.close()

    def test_mail_read_during_classification_does_not_claim_agent_was_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            telegram = FakeTelegram()
            with patch.object(MailSource, "scan", side_effect=[[email()], []]), \
                 patch.object(MailSource, "refresh", return_value={email()["id"]: True}), \
                 patch.object(Classifier, "classify", return_value=[analysis(email())]) as classify, \
                 patch("mail_digest.service.Telegram", return_value=telegram):
                run(cfg)
            classify.assert_called_once()
            self.assertEqual(telegram.calls, ["message", "document"])
            self.assertNotIn("未启动 Agent", telegram.messages[0])
            self.assertIn("状态：已读", telegram.messages[0])

    def test_already_sent_today_does_not_refetch_or_send(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            cfg.state_dir.mkdir()
            store = Store(cfg.state_dir / "state.sqlite3")
            with store.db:
                store.put("last_report_date", datetime.now(ZoneInfo(cfg.timezone)).date().isoformat())
            store.db.close()
            with patch.object(MailSource, "scan") as scan, patch("mail_digest.service.Telegram") as telegram:
                run(cfg)
                scan.assert_not_called()
                telegram.return_value.message.assert_not_called()


class ClassifierIsolationTests(unittest.TestCase):
    def fake_cli(self, folder, tool=False):
        cfg = config(folder)
        cfg.auth_home.mkdir()
        (cfg.auth_home / "auth.json").write_text('{}')
        path = Path(folder) / "fake-codex"
        payload = {"items": [analysis(email())]}
        code = ("#!/usr/bin/python3\nimport json,sys,os\n"
                "assert not any(k in os.environ for k in ['SUPABASE_SERVICE_ROLE_KEY','TELEGRAM_BOT_TOKEN'])\n"
                "assert '--ignore-user-config' in sys.argv\n"
                "assert 'features.shell_tool=false' in sys.argv\n"
                "prompt=sys.stdin.read();assert '不可信' in prompt\n"
                "answer=sys.argv[sys.argv.index('--output-last-message')+1]\n"
                f"open(answer,'w').write({json.dumps(json.dumps(payload, ensure_ascii=False))})\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'error','message':'feature warning'}}))\n")
        if tool:
            code += "print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call'}}))\n"
        path.write_text(code)
        path.chmod(0o700)
        from dataclasses import replace
        return replace(cfg, codex_bin=str(path))

    def test_credentials_and_user_config_are_not_forwarded(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = self.fake_cli(folder)
            with patch.dict('os.environ', {'SUPABASE_SERVICE_ROLE_KEY': 'sensitive', 'TELEGRAM_BOT_TOKEN': 'sensitive'}):
                items = Classifier(cfg).classify([email()], [])
            self.assertEqual(items[0]['priority'], 'P1')
            self.assertEqual(list(cfg.state_dir.iterdir()), [])

    def test_unexpected_tool_use_rejects_report(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = self.fake_cli(folder, tool=True)
            with self.assertRaisesRegex(DigestError, 'unexpected_tool_use'):
                Classifier(cfg).classify([email()], [])


class ControlTests(unittest.TestCase):
    def test_confirm_received_part_skips_only_that_part(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            store = Store(path / 'state.sqlite3')
            key = store.enqueue({'summary':'text'})
            with store.db:
                store.db.execute("UPDATE outbox SET status='uncertain' WHERE id=?", (key,))
            with patch('builtins.print'):
                control_store(path, 'skip')
            self.assertEqual(store.pending()['next_part'], 1)
            self.assertEqual(store.pending()['status'], 'pending')
            store.db.close()

    def test_failure_alert_is_sent_once_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            cfg.state_dir.mkdir()
            with patch('mail_digest.service.Telegram') as telegram:
                alert_failure(cfg, 'codex_timeout')
                alert_failure(cfg, 'systemd_service_failed')
                self.assertEqual(telegram.return_value.message.call_count, 1)
                text = telegram.return_value.message.call_args.args[0]
                self.assertNotIn(cfg.db_key, text)
                self.assertNotIn(cfg.telegram_token, text)
                self.assertIn('codex_timeout', text)


if __name__ == "__main__":
    unittest.main()
