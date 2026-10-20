from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from growth_digest.service import (
    Notion, Source, aggregate, analyze, change, render, run, window,
)
from mail_digest.service import AmbiguousDelivery, DigestError, Store
from test_mail_digest import FakeTelegram, config


def uid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def sample():
    target = date(2026, 10, 20)
    now = datetime(2026, 10, 21, 0, 15, tzinfo=UTC)
    data = {key: [] for key in ["users", "tasks", "logins", "downloads", "payments", "completions", "first_values", "backlog"]}
    for n in range(28):
        day = target - timedelta(days=n)
        ts = str(day) + "T04:00:00+00:00"
        for j in range(10):
            number = n * 10 + j + 1
            data["users"].append({"id": uid(number), "created_at": ts, "acquisition_channel": "search"})
            data["tasks"].append({"id": uid(1000 + number), "user_id": uid(number), "created_at": ts,
                                  "status": "completed", "deleted_at": None})
    return data, target, now


class MetricsTests(unittest.TestCase):
    def test_complete_day_shanghai_and_dst(self):
        target, start, end = window(datetime(2026, 10, 21, 0, 15, tzinfo=UTC), "Asia/Shanghai")
        self.assertEqual(str(target), "2026-10-20")
        self.assertEqual(end.isoformat(), "2026-10-20T16:00:00+00:00")
        _, start, end = window(datetime(2026, 3, 9, 12, tzinfo=UTC), "America/New_York")
        # Includes the spring-forward day, so 28 local days are one UTC hour shorter.
        self.assertEqual((end-start).total_seconds(), 28 * 86400 - 3600)

    def test_same_day_conversion_unique_users_and_soft_deleted_tasks(self):
        data, target, now = sample()
        data["tasks"].append(data["tasks"][0] | {"id": uid(9999), "deleted_at": "2026-10-20T05:00:00"})
        data["tasks"].append(data["tasks"][0] | {"id": uid(9998), "user_id": uid(11)})
        metrics = aggregate(data, target, now, "Asia/Shanghai")
        current = metrics["daily"][-1]
        self.assertEqual(current["new_tasks"], 12)
        self.assertEqual(current["task_users"], 11)
        self.assertEqual(current["new_user_submitters"], 10)
        self.assertEqual(metrics["new_user_same_day_submit_percent"], 100)
        self.assertEqual(metrics["comparisons"]["new_users"]["week_totals"]["value"], 70)
        self.assertNotIn(uid(1), json.dumps(metrics))

    def test_volume_alert_requires_both_absolute_and_relative_change(self):
        data, target, now = sample()
        data["users"] = [r for r in data["users"] if r["created_at"][:10] != str(target)]
        metrics = aggregate(data, target, now, "Asia/Shanghai")
        self.assertTrue(any("新增用户" in a and "下降" in a for a in metrics["alerts"]))
        self.assertIsNone(change(5, 0)["percent"])
        self.assertEqual(change(0, 0)["delta"], 0)

    def test_failures_use_resolved_cohort_and_backlog_is_query_snapshot(self):
        data, target, now = sample()
        for row in data["tasks"][:3]:
            row["status"] = "failed"
        data["backlog"] = [{"created_at": "2026-10-18T00:00:00", "updated_at": "2026-10-18T00:00:00"}]
        metrics = aggregate(data, target, now, "Asia/Shanghai")
        self.assertEqual(metrics["resolved_cohort_failure_percent"], 30)
        self.assertEqual(len(metrics["alerts"]), 2)
        self.assertEqual(metrics["backlog_now"]["older_than_24h"], 1)
        report = render(metrics, {k: "需核查" for k in ["overview", "growth", "anomalies", "actions"]})[1]
        self.assertIn("28天日级数据", report)
        self.assertIn("非 DAU", report)

    def test_midnight_utc_rows_bucket_into_local_day(self):
        data, target, now = sample()
        data["logins"] = [{"created_at": "2026-10-19T16:00:00Z", "user_id": uid(1)},
                           {"created_at": "2026-10-20T15:59:59Z", "user_id": uid(1)}]
        self.assertEqual(aggregate(data, target, now, "Asia/Shanghai")["daily"][-1]["login_users"], 1)


class SourceTests(unittest.TestCase):
    def test_short_server_capped_page_does_not_stop_pagination(self):
        source = Source(config("/tmp"))
        page = [{"id": uid(1), "created_at": "2026-10-20T00:00:00"}]
        with patch.object(source, "get", side_effect=[page, [page[0] | {"id": uid(2)}], []]) as get:
            rows = source.scan("tasks", "id,created_at", None, datetime(2026, 10, 21, tzinfo=UTC))
        self.assertEqual(len(rows), 2)
        self.assertIn("id.gt." + uid(1), get.call_args_list[1].args[1]["or"])

    def test_pagination_repeat_and_outside_bounds_fail_closed(self):
        source = Source(config("/tmp"))
        row = {"id": uid(1), "created_at": "2026-10-20T00:00:00"}
        with patch.object(source, "get", side_effect=[[row], [row]]):
            with self.assertRaisesRegex(DigestError, "stalled"):
                source.scan("tasks", "id,created_at", None, datetime(2026, 10, 21, tzinfo=UTC))
        with patch.object(source, "get", return_value=[row]):
            with self.assertRaisesRegex(DigestError, "outside_window"):
                source.scan("tasks", "id,created_at", None, datetime(2026, 10, 20, tzinfo=UTC))

    def test_collection_excludes_admin_across_sources_and_queries_event_times(self):
        source = Source(config("/tmp"))
        data, target, now = sample()
        def scan(table, cols, start, end, field="created_at", filters=None):
            if filters and filters.get("is_admin"):
                return [{"id": uid(1)}]
            if table == "users" and field == "created_at":
                return data["users"]
            if table == "tasks" and filters == {}:
                return data["tasks"]
            return []
        with patch.object(source, "scan", side_effect=scan) as fetch:
            metrics = source.collect(now)
        self.assertEqual(metrics["daily"][-1]["new_users"], 9)
        self.assertEqual(metrics["daily"][-1]["new_tasks"], 9)
        self.assertTrue(any(c.kwargs.get("field") == "paid_at" for c in fetch.call_args_list))

    def test_invalid_agent_result_does_not_become_report(self):
        with patch('growth_digest.service.Classifier.generate', return_value={"overview": "invented"}):
            with self.assertRaises(DigestError):
                analyze(config("/tmp"), {})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = config(self.temp.name)
        data, target, now = sample()
        self.metrics = aggregate(data, target, now, "Asia/Shanghai")
        # Keep the fixed historical fixture aligned with today's scheduled report date.
        self.date = str(window(datetime.now(UTC), self.config.timezone)[0])
        self.metrics["report_date"] = self.date
        self.analysis = {k: "数据需验证" for k in ["overview", "growth", "anomalies", "actions"]}
        self.env = patch.dict('os.environ', {"GROWTH_DIGEST_NOTION_PAGE_ID": uid(100)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_dry_run_no_publication_then_cached_send_and_daily_dedupe(self):
        telegram = FakeTelegram()
        with patch('growth_digest.service.Source.collect', return_value=self.metrics) as source, \
             patch('growth_digest.service.analyze', return_value=self.analysis) as agent, \
             patch('growth_digest.service.Notion.publish', return_value='https://notion.so/report') as notion, \
             patch('growth_digest.service.GrowthTelegram', return_value=telegram), \
             patch('mail_digest.service.time.sleep'):
            run(self.config, dry_run=True)
            notion.assert_not_called()
            self.assertEqual(telegram.calls, [])
            run(self.config)
            run(self.config)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(agent.call_count, 1)
            self.assertEqual(notion.call_count, 1)
            self.assertEqual(telegram.calls, ["message", "document"])

    def test_notion_failure_keeps_cache_and_never_sends(self):
        with patch('growth_digest.service.Source.collect', return_value=self.metrics) as source, \
             patch('growth_digest.service.analyze', return_value=self.analysis) as agent, \
             patch('growth_digest.service.Notion.publish', side_effect=DigestError('notion_failed')), \
             patch('growth_digest.service.GrowthTelegram') as telegram:
            for _ in range(2):
                with self.assertRaisesRegex(DigestError, 'notion_failed'):
                    run(self.config)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(agent.call_count, 1)
            telegram.assert_not_called()

    def test_uncertain_send_blocks_automatic_resend(self):
        telegram = FakeTelegram({1: AmbiguousDelivery('uncertain')})
        with patch('growth_digest.service.Source.collect', return_value=self.metrics), \
             patch('growth_digest.service.analyze', return_value=self.analysis), \
             patch('growth_digest.service.Notion.publish', return_value='https://notion.so/report'), \
             patch('growth_digest.service.GrowthTelegram', return_value=telegram):
            with self.assertRaises(AmbiguousDelivery):
                run(self.config)
            with self.assertRaisesRegex(DigestError, 'confirmation'):
                run(self.config)
            self.assertEqual(telegram.calls, ["message"])


class NotionTests(unittest.TestCase):
    def test_update_only_generated_section_preserving_user_content(self):
        n = Notion()
        page = 'before\n【自动日报开始】\nold\n【自动日报结束】\nafter'
        calls = []
        def request(method, params, **kwargs):
            nonlocal page
            calls.append((method, params))
            if method == 'tools/call' and params['name'] == 'notion-update-page':
                operation = params['arguments']['content_updates'][0]
                page = page.replace(operation['old_str'], operation['new_str'])
            if method == 'tools/list':
                return {'tools': [{'name': 'notion-fetch'}, {'name': 'notion-update-page'}]}
            if method == 'tools/call' and params['name'] == 'notion-fetch':
                return {'content': [{'text': json.dumps({'text': page})}]}
            return {}
        with patch.object(n, 'request', side_effect=request):
            n.publish(uid(100), 'new')
        updates = next(p['arguments']['content_updates'][0] for m, p in calls if m == 'tools/call' and p['name'] == 'notion-update-page')
        self.assertEqual(updates['old_str'], '【自动日报开始】\nold\n【自动日报结束】')
        self.assertNotIn('before', updates['new_str'])
