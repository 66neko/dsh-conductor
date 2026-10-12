"""将本次运行的报告正文与可信 verdict 合成为稳定的展示快照。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import OperationError
from .lifecycle import Budget
from .models import ExecutionPlan, RecordError, Verdict, WorkerReceipt, _read_text, read_json_object
from .state import RunState


_AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    text: str
    path: Path | None
    warnings: tuple[str, ...]


def _within(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RecordError("报告路径越出本轮目录")
    except RuntimeError as exc:
        raise RecordError("报告路径包含循环链接") from exc
    return resolved


def _body(path: Path, root: Path, budget: Budget) -> str:
    text = _read_text(_within(path, root), budget, preserve_newlines=True)
    if not text.strip():
        raise RecordError("报告正文为空")
    return text


def _verification(verdict: Verdict | None, failure: str | None) -> str:
    if verdict is None:
        return "\n## 验收结果\n\n未形成有效验收结论。\n\n" + (f"运行错误：{failure}\n" if failure else "")

    status_text = "✓ 通过" if verdict.status == "accepted" else "✗ 未通过"
    parts = [f"\n## 验收结果\n\n**{status_text}**\n\n{verdict.summary}\n"]

    if verdict.remaining_issues:
        parts.append("\n**剩余问题：**\n" + "\n".join(f"- {issue}" for issue in verdict.remaining_issues) + "\n")

    if failure:
        parts.append(f"\n运行错误：{failure}\n")

    return "".join(parts)


def _persist(root: Path, text: str, budget: Budget) -> Path:
    budget.check()
    fd, name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            for offset in range(0, len(text), 65536):
                budget.check()
                handle.write(text[offset:offset + 65536])
        budget.check()
        path = root / "report.md"
        os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def build_report(state: RunState, *, plan: ExecutionPlan | None, verdict: Verdict | None,
                 budget: Budget, failure: str | None = None, partial: bool = False) -> ReportSnapshot:
    """收集各轮 result.md 和验收结论；文件问题以警告展示。"""
    warnings: list[str] = []
    parts: list[str] = []

    try:
        budget.check()
        if plan is None:
            plan = ExecutionPlan.load(_within(state.plan_file, state.root), expected_run_id=state.run_id, budget=budget)

        selected = state.agent_state(plan.agent)
        count = verdict.attempts if verdict else 0

        # 尝试从监督记录获取实际轮次
        supervision_path = state.root / "supervision.json"
        if supervision_path.exists():
            try:
                supervision = read_json_object(_within(supervision_path, state.root), budget=budget)
                if (supervision.get("run_id"), supervision.get("agent"), supervision.get("session")) != (
                        state.run_id, plan.agent.value, selected.session):
                    raise RecordError("监督记录身份不匹配")
                number = supervision.get("attempt")
                if type(number) is not int or not 0 <= number <= len(selected.attempts):
                    raise RecordError("监督记录轮次非法")
                if verdict is not None and number != verdict.attempts:
                    warnings.append("实际登记轮次与 verdict.attempts 不一致。")
                count = max(count, number)
            except (OSError, ValueError, RecordError) as exc:
                warnings.append(f"无法核对实际执行轮次：{exc}")
        elif verdict is None:
            warnings.append("没有实际执行轮次记录。")

        if count == 0:
            parts.append("## 执行记录\n\n没有已确认执行的任务轮次。\n")
        else:
            parts.append(f"## 执行记录\n\n使用 {_AGENT_NAMES[plan.agent.value]} 执行，共 {count} 轮。\n")

        # 收集各轮 result.md
        for attempt in selected.attempts[:count]:
            budget.check()
            try:
                _within(attempt.result_file.parent, state.root)
                _within(attempt.receipt_file, attempt.result_file.parent)
                _within(attempt.result_file, attempt.result_file.parent)
                WorkerReceipt.load(attempt.receipt_file, expected_token=attempt.token, budget=budget)
            except (OSError, ValueError, RecordError) as exc:
                warnings.append(f"第 {attempt.number} 轮未确认有效交接：{exc}")

            try:
                _within(attempt.result_file.parent, state.root)
                if count > 1:  # 多轮时显示轮次标题
                    parts.append(f"\n### 第 {attempt.number} 轮\n\n")
                parts.append(_body(attempt.result_file, attempt.result_file.parent, budget))
                parts.append("\n")
            except (OSError, ValueError, RecordError) as exc:
                message = f"第 {attempt.number} 轮报告缺失或不可读：{exc}"
                warnings.append(message)
                parts.append(f"\n> {message}\n")

    except OperationError as exc:
        if not partial:
            raise
        warnings.append(f"剩余预算不足或操作已停止，报告收集未完成：{exc}")
    except (OSError, ValueError, RecordError) as exc:
        warnings.append(f"报告收集未完成：{exc}")

    if warnings:
        parts.append("\n---\n\n" + "\n".join(f"⚠ {message}" for message in warnings) + "\n")

    parts.append(_verification(verdict, failure))

    text = "".join(parts)
    path = None
    try:
        path = _persist(state.root, text, budget)
    except OperationError as exc:
        if not partial:
            raise
        warnings.append(f"合并报告未落盘：{exc}")
    except OSError as exc:
        warnings.append(f"合并报告未落盘：{exc}")

    return ReportSnapshot(text, path, tuple(warnings))
