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
from conductor.models import AcceptanceCriterion, AgentKind, ExecutionPlan, Verdict, VerificationCheck, WorkerReceipt
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
        self.plan = ExecutionPlan(1, self.state.run_id, AgentKind.CODEX, "指定", "实现并测试中文功能",
                                  ("实现", "测试"), (AcceptanceCriterion("test", "检查通过"),))
        atomic_write_json(self.state.plan_file, self.plan.to_json())
        self.verdict = Verdict(2, self.state.run_id, "accepted", AgentKind.CODEX, 2, ("output.txt",),
                               (VerificationCheck("test", "检查通过", "python -m unittest", "独立执行通过", True),),
                               "全部验收通过", ())
        self.selected = self.state.agent_state(AgentKind.CODEX)
        self.budget = Budget(float("inf"))
        for attempt in self.selected.attempts[:2]:
            attempt.result_file.write_text(f"第 {attempt.number} 轮原始完整正文\n", encoding="utf-8")
            atomic_write_json(attempt.receipt_file, WorkerReceipt(attempt.token, "ready_for_verification", "已完成").to_json())
            self.manifest(attempt.number, [])

    def manifest(self, number, tasks, **overrides):
        attempt = self.selected.attempts[number - 1]
        atomic_write_json(attempt.result_file.with_name("subtask-reports.json"), {
            "schema_version": 1, "receipt_token": attempt.token, "subtasks": tasks, **overrides,
        })

    def subtask(self, task_id, *, number=1, parent=None, body="子任务全部正文", **overrides):
        root = self.selected.attempts[number - 1].result_file.parent
        (root / "subtasks").mkdir(exist_ok=True)
        (root / "subtasks" / f"{task_id}.md").write_text(body, encoding="utf-8")
        return {"id": task_id, "parent_id": parent, "agent": "claude", "title": "检查实现",
                "status": "completed", "report_file": f"subtasks/{task_id}.md", **overrides}

    def report(self, **kwargs):
        return build_report(self.state, plan=self.plan, verdict=kwargs.pop("verdict", self.verdict),
                            budget=kwargs.pop("budget", self.budget), **kwargs)

    def test_all_attempts_nested_subtasks_and_long_unicode_bodies_are_preserved(self):
        long_body = "\n".join(f"ROW-{i:05d} 中文 🐈" for i in range(6001)) + "\nEND-OF-RESULT"
        first = self.subtask("parent", body=long_body)
        child = self.subtask("child", parent="parent", agent="codex", body="后代报告原文")
        self.manifest(1, [first, child])
        # 未使用轮次和另一候选 worker 的报告不属于本次结果。
        self.selected.attempts[2].result_file.write_text("未使用轮次诱饵")
        self.state.agent_state(AgentKind.CLAUDE).attempts[0].result_file.write_text("未选择 agent 诱饵")
        result = self.report()
        self.assertFalse(result.warnings)
        self.assertIn(long_body, result.text)
        self.assertIn("后代报告原文", result.text)
        self.assertLess(result.text.index("第 1 轮原始"), result.text.index("第 2 轮原始"))
        self.assertNotIn("诱饵", result.text)
        self.assertIn("### Codex 第 1 轮报告（历史轮次）", result.text)
        self.assertIn("### Codex 第 2 轮报告", result.text)
        self.assertIn("### 子任务 parent — Claude Code：检查实现", result.text)
        self.assertIn("### 子任务 child — Codex：检查实现", result.text)
        self.assertNotIn("回执状态：", result.text)
        self.assertNotIn("父任务：", result.text)
        self.assertNotIn("自报状态：", result.text)
        self.assertNotIn("来源：", result.text)
        self.assertNotIn(self.state.run_id, result.text)
        self.assertNotIn(self.plan.task_summary, result.text)
        self.assertNotIn("python -m unittest", result.text)
        self.assertNotIn("独立执行通过", result.text)
        self.assertNotIn("output.txt", result.text)
        self.assertNotIn("### 剩余问题", result.text)
        self.assertTrue(result.text.endswith("最终结论：通过（accepted）\n\n全部验收通过\n"))
        self.assertEqual(result.path.read_text(encoding="utf-8"), result.text)
        self.assertEqual(json.loads(json.dumps({"report": result.text}))["report"], result.text)

    def test_original_markdown_is_preserved_even_when_it_uses_report_headings(self):
        worker_body = ("## 一、所有任务结果报告\n\nworker 原始说明\n\n"
                       "## 二、任务验收报告\n\nworker 自查结果\n\n"
                       "### 产物\n\n- worker-output.txt\n")
        subtask_body = "## 二、任务验收报告\n\n子任务原始检查方法：运行测试\n"
        self.selected.attempts[0].result_file.write_text(worker_body, encoding="utf-8")
        self.manifest(1, [self.subtask("same-headings", body=subtask_body)])
        result = self.report()
        self.assertIn(worker_body, result.text)
        self.assertIn(subtask_body, result.text)
        self.assertEqual(result.text.count(worker_body), 1)
        self.assertEqual(result.text.count(subtask_body), 1)
        self.assertEqual(result.text.count("最终结论：通过（accepted）"), 1)
        self.assertTrue(result.text.endswith("最终结论：通过（accepted）\n\n全部验收通过\n"))

    def test_rejected_collects_registered_partial_attempt_without_receipt(self):
        atomic_write_json(self.state.root / "supervision.json", {
            "run_id": self.state.run_id, "agent": "codex", "session": self.selected.session, "attempt": 3,
        })
        self.selected.attempts[2].result_file.write_text("中途失败仍然保留的正文", encoding="utf-8")
        rejected = replace(self.verdict, status="rejected", summary="任务未完成", remaining_issues=("任务阻塞",))
        result = self.report(verdict=rejected)
        self.assertIn("中途失败仍然保留的正文", result.text)
        self.assertIn("未通过（rejected）", result.text)
        self.assertIn("任务未完成", result.text)
        self.assertIn("### 剩余问题\n\n- 任务阻塞", result.text)
        self.assertTrue(any("未确认有效交接" in item for item in result.warnings))
        self.assertTrue(any("轮次与" in item for item in result.warnings))

    def test_runtime_failure_preserves_a_valid_business_conclusion(self):
        result = self.report(failure="cleanup failed")
        self.assertIn("最终结论：通过（accepted）", result.text)
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

    def test_manifest_identity_and_missing_files_are_warnings_not_acceptance_changes(self):
        self.manifest(1, [self.subtask("a", body="不属于本轮的子报告")], receipt_token="wrong")
        self.selected.attempts[1].result_file.unlink()
        result = self.report()
        self.assertNotIn("不属于本轮的子报告", result.text)
        self.assertIn("报告缺失或不可读", result.text)
        self.assertIn("最终结论：通过", result.text)
        self.assertTrue(result.warnings)

    def test_manifest_rejects_escape_symlinks_duplicates_invalid_parents_and_fifo(self):
        outside = self.root / "private.txt"
        outside.write_text("禁止读取的内容")
        first = self.subtask("good", body="只出现一次的子报告")
        escape = self.subtask("escape", report_file="../../private.txt")
        link = self.subtask("link")
        link_path = self.selected.attempts[0].result_file.parent / link["report_file"]
        link_path.unlink()
        link_path.symlink_to(outside)
        fifo = self.subtask("fifo")
        fifo_path = link_path.with_name("fifo.md")
        fifo_path.unlink()
        os.mkfifo(fifo_path)
        invalid_parent = self.subtask("orphan", parent="unknown")
        duplicate_file = self.subtask("samefile", report_file=first["report_file"])
        self.manifest(1, [first, first, escape, link, fifo, invalid_parent, duplicate_file])
        result = self.report()
        self.assertEqual(result.text.count("只出现一次的子报告"), 1)
        self.assertNotIn("禁止读取的内容", result.text)
        self.assertGreaterEqual(len(result.warnings), 6)

    def test_unfinished_subtask_and_corrupt_manifest_remain_visible(self):
        self.manifest(1, [self.subtask("blocked", status="blocked", body="阻塞前的工作")])
        self.selected.attempts[1].result_file.with_name("subtask-reports.json").write_text("not json")
        result = self.report()
        self.assertIn("阻塞前的工作", result.text)
        self.assertTrue(any("blocked" in item for item in result.warnings))
        self.assertTrue(any("清单不可用" in item for item in result.warnings))

    def test_deadline_and_cancel_propagate_normally_but_partial_errors_keep_original(self):
        cancelled = threading.Event()
        cancelled.set()
        for budget in (Budget(0), Budget(float("inf"), cancel_event=cancelled)):
            with self.subTest(budget=budget):
                with self.assertRaises(OperationError):
                    self.report(budget=budget)
                result = self.report(budget=budget, verdict=None, failure="original failure", partial=True)
                self.assertIn("original failure", result.text)
                self.assertIn("未形成可返回的有效验收结论", result.text)
                self.assertIsNone(result.path)
                self.assertTrue(result.warnings)

    def test_file_write_failure_keeps_inline_report(self):
        with mock.patch("conductor.reports.os.replace", side_effect=OSError("disk full")):
            result = self.report()
        self.assertIsNone(result.path)
        self.assertIn("第 1 轮原始完整正文", result.text)
        self.assertTrue(any("disk full" in item for item in result.warnings))
        self.assertFalse(list(self.state.root.glob(".report-*.tmp")))


if __name__ == "__main__":
    unittest.main()
