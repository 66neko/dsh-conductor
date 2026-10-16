from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from conductor.errors import OperationError
from conductor.lifecycle import Budget
from conductor.models import AgentKind, ExecutionPlan, Verdict, WorkerReceipt
from conductor.reports import build_report
from conductor.state import RunState, atomic_write_json


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = RunState.create(state_root=self.root / "state", workspace=self.root, prompt="报告任务",
                                     available_agents=set(AgentKind), max_attempts=3,
                                     worker_idle_timeout_seconds=300, keep_session=False, include_report=True)
        self.plan = ExecutionPlan(1, self.state.run_id, AgentKind.CODEX, "指定", "实现并测试中文功能")
        atomic_write_json(self.state.plan_file, self.plan.to_json())
        self.verdict = Verdict(3, self.state.run_id, "accepted", AgentKind.CODEX, 2, "全部验收通过", ())
        self.selected = self.state.agent_state(AgentKind.CODEX)
        self.budget = Budget(float("inf"))
        for attempt in self.selected.attempts[:2]:
            attempt.result_file.write_text(f"第 {attempt.number} 轮原始完整正文\n", encoding="utf-8")
            atomic_write_json(attempt.receipt_file, WorkerReceipt(attempt.token, "ready_for_verification", "已完成").to_json())

    def report(self, **kwargs):
        return build_report(self.state, plan=self.plan, verdict=kwargs.pop("verdict", self.verdict),
                            budget=kwargs.pop("budget", self.budget), **kwargs)

    def test_all_attempts_and_long_unicode_bodies_are_preserved(self):
        long_body = "\n".join(f"ROW-{i:05d} 中文 🐈" for i in range(6001)) + "\nEND-OF-RESULT"
        self.selected.attempts[0].result_file.write_text(long_body, encoding="utf-8")
        # 未使用轮次和另一候选 worker 的报告不属于本次结果。
        self.selected.attempts[2].result_file.write_text("未使用轮次诱饵")
        self.state.agent_state(AgentKind.CLAUDE).attempts[0].result_file.write_text("未选择 agent 诱饵")
        result = self.report()
        self.assertFalse(result.warnings)
        self.assertIn(long_body, result.text)
        self.assertLess(result.text.index("第 1 轮"), result.text.index("第 2 轮"))
        self.assertNotIn("诱饵", result.text)
        self.assertIn("### 第 1 轮", result.text)
        self.assertIn("### 第 2 轮", result.text)
        self.assertNotIn("回执状态：", result.text)
        self.assertNotIn(self.state.run_id, result.text)
        self.assertNotIn(self.plan.task_summary, result.text)
        self.assertNotIn("python -m unittest", result.text)
        self.assertNotIn("独立执行通过", result.text)
        self.assertNotIn("output.txt", result.text)
        self.assertNotIn("**剩余问题：**", result.text)
        self.assertTrue(result.text.endswith("**✓ 通过**\n\n全部验收通过\n"))
        self.assertEqual(result.path.read_text(encoding="utf-8"), result.text)
        self.assertEqual(json.loads(json.dumps({"report": result.text}))["report"], result.text)

    def test_original_markdown_is_preserved_even_when_it_uses_report_headings(self):
        worker_body = ("## 执行记录\n\nworker 原始说明\n\n"
                       "## 验收结果\n\nworker 自查结果\n\n"
                       "### 产物\n\n- worker-output.txt\n")
        self.selected.attempts[0].result_file.write_text(worker_body, encoding="utf-8")
        result = self.report()
        self.assertIn(worker_body, result.text)
        self.assertEqual(result.text.count(worker_body), 1)
        self.assertEqual(result.text.count("**✓ 通过**"), 1)
        self.assertTrue(result.text.endswith("**✓ 通过**\n\n全部验收通过\n"))

    def test_rejected_collects_registered_partial_attempt_without_receipt(self):
        atomic_write_json(self.state.root / "supervision.json", {
            "run_id": self.state.run_id, "agent": "codex", "session": self.selected.session, "attempt": 3,
        })
        self.selected.attempts[2].result_file.write_text("中途失败仍然保留的正文", encoding="utf-8")
        rejected = replace(self.verdict, status="rejected", summary="任务未完成", remaining_issues=("任务阻塞",))
        result = self.report(verdict=rejected)
        self.assertIn("中途失败仍然保留的正文", result.text)
        self.assertIn("**✗ 未通过**", result.text)
        self.assertIn("任务未完成", result.text)
        self.assertIn("**剩余问题：**\n- 任务阻塞", result.text)
        self.assertTrue(any("未确认有效交接" in item for item in result.warnings))
        self.assertTrue(any("轮次与" in item for item in result.warnings))

    def test_runtime_failure_preserves_a_valid_business_conclusion(self):
        result = self.report(failure="cleanup failed")
        self.assertIn("**✓ 通过**", result.text)
        self.assertIn("全部验收通过", result.text)
        self.assertIn("运行错误：cleanup failed", result.text)

    def test_zero_attempts_does_not_collect_precreated_files(self):
        verdict = replace(self.verdict, status="rejected", attempts=0, remaining_issues=("agent unavailable",))
        result = self.report(verdict=verdict)
        self.assertIn("没有已确认执行", result.text)
        self.assertNotIn("原始完整正文", result.text)

    def test_body_preserves_crlf_and_rejects_cross_attempt_links(self):
        first, second = self.selected.attempts[:2]
        first.result_file.write_bytes("原始\r\n换行\r\n".encode("utf-8"))
        second.result_file.unlink()
        second.result_file.symlink_to(first.result_file)
        result = self.report()
        self.assertEqual(result.text.count("原始\r\n换行\r\n"), 1)
        self.assertEqual(result.path.read_bytes().decode("utf-8"), result.text)
        self.assertTrue(any("越出" in warning for warning in result.warnings))

    def test_missing_result_file_is_warning_not_acceptance_change(self):
        self.selected.attempts[1].result_file.unlink()
        result = self.report()
        self.assertIn("报告缺失或不可读", result.text)
        self.assertIn("**✓ 通过**", result.text)
        self.assertTrue(result.warnings)

    def test_deadline_and_cancel_propagate_normally_but_partial_errors_keep_original(self):
        cancelled = threading.Event()
        cancelled.set()
        for budget in (Budget(0), Budget(float("inf"), cancel_event=cancelled)):
            with self.subTest(budget=budget):
                with self.assertRaises(OperationError):
                    self.report(budget=budget)
                result = self.report(budget=budget, verdict=None, failure="original failure", partial=True)
                self.assertIn("original failure", result.text)
                self.assertIn("未形成有效验收结论", result.text)
                self.assertIsNone(result.path)
                self.assertTrue(result.warnings)

    def test_file_write_failure_keeps_inline_report(self):
        with mock.patch("conductor.reports.os.replace", side_effect=OSError("disk full")):
            result = self.report()
        self.assertIsNone(result.path)
        self.assertIn("第 1 轮原始完整正文", result.text)
        self.assertTrue(any("disk full" in item for item in result.warnings))
        self.assertFalse(list(self.state.root.glob(".report-*.tmp")))
