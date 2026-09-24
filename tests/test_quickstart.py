from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from conductor import ConductorError, TaskResult
from conductor.models import AcceptanceCriterion, AgentKind, ExecutionPlan, Verdict, VerificationCheck
from conductor.sdk import DshRunSummary
from conductor.tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxSession
from examples import quickstart


def make_result(workspace: Path, *, report: str | None = None) -> TaskResult:
    (workspace / "hello.py").write_text("print('Hello, DSH!')\n", encoding="utf-8")
    worker_result = workspace / "result.md"
    worker_result.write_text("已创建 hello.py 并检查输出。\nEND-OF-RESULT\n", encoding="utf-8")
    criterion = AcceptanceCriterion("output", "hello.py 的输出、退出码及 stderr 符合要求")
    return TaskResult(
        run_id="quickstart-test", workspace=workspace, state_directory=workspace / "run",
        session="quickstart-test", worker_result=worker_result, report=report,
        plan=ExecutionPlan(
            schema_version=1, run_id="quickstart-test", agent=AgentKind.CLAUDE,
            agent_reason="测试指定", task_summary="创建 hello.py", implementation_steps=("创建脚本",),
            acceptance_criteria=(criterion,),
        ),
        verdict=Verdict(
            schema_version=2, run_id="quickstart-test", status="accepted", agent=AgentKind.CLAUDE,
            attempts=1, artifacts=("hello.py",), summary="脚本通过独立检查", remaining_issues=(),
            checks=(VerificationCheck(criterion.id, criterion.description, "运行脚本", "输出符合要求", True),),
        ),
        dsh=DshRunSummary(status="idle", elapsed_seconds=1.0, event_count=1, final_text="已完成"),
    )


class QuickstartProbeTests(unittest.TestCase):
    def run_probe(self, workspace: Path, lines: list[str]) -> None:
        session = mock.Mock(spec=TmuxSession)
        session.name = "quickstart-probe"
        session.capture.side_effect = lambda *, history_lines=0: "\n".join(lines[-(history_lines + 50):]) + "\n"
        session.exists.return_value = True
        with (
            mock.patch("examples.quickstart.TmuxSession.create", return_value=session),
            mock.patch("examples.quickstart.subprocess.run", return_value=mock.Mock(stdout=f"{HISTORY_LIMIT}\n")),
            mock.patch("conductor.worker_log.TmuxSession", return_value=session),
            redirect_stdout(io.StringIO()),
        ):
            try:
                quickstart.check_tmux(workspace)
            finally:
                session.close.assert_called_once()

    def test_history_and_persisted_log_accept_tmux_trailing_padding(self) -> None:
        expected = [f"PROBE-{i:05d}" for i in range(1, 6001)]
        # tmux 3.2a 的 capture-pane -J 会在完整行后保留填充空格。
        for padding in ("", " " * 39):
            with self.subTest(padding=len(padding)), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                self.run_probe(workspace, [line + padding for line in expected])
                persisted = [
                    line[2:] for line in (workspace / "tmux-probe.log").read_text(encoding="utf-8").splitlines()
                    if line.startswith("| ")
                ]
                self.assertEqual(persisted, expected[-(DEFAULT_CAPTURE_HISTORY_LINES + 50):])

    def test_history_still_rejects_missing_duplicate_reordered_and_blank_lines(self) -> None:
        for defect in ("missing", "duplicate", "reordered", "blank"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lines = [f"PROBE-{i:05d}   " for i in range(1, 6001)]
                if defect == "missing":
                    del lines[100]
                elif defect == "duplicate":
                    lines[100] = lines[99]
                elif defect == "reordered":
                    lines[100], lines[101] = lines[101], lines[100]
                else:
                    lines.insert(100, "   ")
                with self.assertRaisesRegex(RuntimeError, "tmux 全量历史缺行或顺序错误"):
                    self.run_probe(Path(directory), lines)


class QuickstartResultTests(unittest.TestCase):
    def test_caller_runs_real_script_and_checks_exact_output_exit_code_and_stderr(self) -> None:
        cases = (
            ("print('Hello, DSH!')\n", None),
            ("print('Hello, DSH!', end='')\n", "stdout 内容不正确"),
            ("print('Hello, DSH!\\n')\n", "stdout 内容不正确"),
            ("import sys; sys.stdout.buffer.write(b'Hello, DSH!\\r\\n')\n", "stdout 内容不正确"),
            ("import sys; print('Hello, DSH!'); print('warning', file=sys.stderr)\n", "stderr 不为空"),
            ("import sys; print('Hello, DSH!'); sys.exit(1)\n", "执行失败"),
        )
        for source, error in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                result = make_result(workspace)
                (workspace / "hello.py").write_text(source, encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    if error is None:
                        quickstart.check_worker_result(result)
                    else:
                        with self.assertRaisesRegex(RuntimeError, error):
                            quickstart.check_worker_result(result)

    def test_caller_rejects_missing_or_incomplete_worker_report(self) -> None:
        for content in (None, "", "实现完成", "错误前缀-END-OF-RESULT"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                result = make_result(Path(directory))
                if content is None:
                    result = replace(result, worker_result=None)
                else:
                    result.worker_result.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "worker_result 路径|末尾标记缺失"):
                    quickstart.check_worker_result(result)


class QuickstartMainTests(unittest.TestCase):
    def test_report_is_on_by_default_and_supports_explicit_on_and_off(self) -> None:
        for args, enabled in (([], True), (["--include-report"], True), (["--no-include-report"], False)):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                report = "Claude Code 原始报告\n\n验收结论：通过。" if enabled else None
                result = make_result(workspace, report=report)
                if enabled:
                    result = replace(result, report_warnings=("报告文件未落盘，正文仍可返回。",))
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch("examples.quickstart.sys.argv", ["quickstart.py", *args]),
                    mock.patch("examples.quickstart.tempfile.mkdtemp", return_value=directory),
                    mock.patch("examples.quickstart.Conductor") as conductor,
                    mock.patch("examples.quickstart.check_tmux") as probe,
                    redirect_stdout(stdout), redirect_stderr(stderr),
                ):
                    conductor.return_value.run.return_value = result
                    self.assertEqual(quickstart.main(), 0)
                self.assertEqual(conductor.call_args.args[1].include_report, enabled)
                probe.assert_not_called()
                self.assertEqual(json.loads((workspace / "quickstart-result.json").read_text()), result.to_json())
                self.assertNotIn("报告文件：None", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")
                if enabled:
                    self.assertIn(report, stdout.getvalue())
                    self.assertIn("报告收集提示：报告文件未落盘", stdout.getvalue())
                else:
                    self.assertNotIn("验收结论：通过。", stdout.getvalue())
                    self.assertNotIn("report", json.loads((workspace / "quickstart-result.json").read_text()))

    def test_optional_report_file_is_displayed_without_rereading_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = replace(make_result(workspace, report="SDK 返回的正文快照"),
                             report_file=workspace / "removed-report.md")
            stdout = io.StringIO()
            with (
                mock.patch("examples.quickstart.sys.argv", ["quickstart.py"]),
                mock.patch("examples.quickstart.tempfile.mkdtemp", return_value=directory),
                mock.patch("examples.quickstart.Conductor") as conductor,
                redirect_stdout(stdout), redirect_stderr(io.StringIO()),
            ):
                conductor.return_value.run.return_value = result
                self.assertEqual(quickstart.main(), 0)
            self.assertIn("SDK 返回的正文快照", stdout.getvalue())
            self.assertIn(f"报告文件：{result.report_file}", stdout.getvalue())

    def test_error_keeps_json_and_displays_available_report_fields(self) -> None:
        for report, has_file, warnings in (
            (None, False, ()),
            ("未完成的原始报告", False, ("本轮报告不完整",)),
            ("未完成的原始报告", True, ()),
        ):
            with self.subTest(report=report, has_file=has_file), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                error = ConductorError("执行超时", code="timeout", state_directory=workspace / "run")
                error.report = report
                error.report_file = workspace / "report.md" if has_file else None
                error.report_warnings = warnings
                stderr = io.StringIO()
                with (
                    mock.patch("examples.quickstart.sys.argv", ["quickstart.py"]),
                    mock.patch("examples.quickstart.tempfile.mkdtemp", return_value=directory),
                    mock.patch("examples.quickstart.Conductor") as conductor,
                    redirect_stdout(io.StringIO()), redirect_stderr(stderr),
                ):
                    conductor.return_value.run.side_effect = error
                    self.assertEqual(quickstart.main(), 1)
                self.assertEqual(json.loads((workspace / "quickstart-error.json").read_text()), error.to_json())
                self.assertNotIn("报告文件：None", stderr.getvalue())
                if report is not None:
                    self.assertIn(report, stderr.getvalue())
                if has_file:
                    self.assertIn(f"报告文件：{error.report_file}", stderr.getvalue())
                for warning in warnings:
                    self.assertIn(f"报告收集提示：{warning}", stderr.getvalue())

    def test_tmux_only_runs_probe_without_starting_conductor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("examples.quickstart.sys.argv", ["quickstart.py", "--tmux-only"]),
                mock.patch("examples.quickstart.tempfile.mkdtemp", return_value=directory),
                mock.patch("examples.quickstart.Conductor") as conductor,
                mock.patch("examples.quickstart.check_tmux") as probe,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(quickstart.main(), 0)
            probe.assert_called_once_with(Path(directory))
            conductor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
