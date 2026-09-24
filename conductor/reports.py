"""将本次运行的报告正文与可信 verdict 合成为稳定的展示快照。"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import OperationError
from .lifecycle import Budget
from .models import ExecutionPlan, RecordError, Verdict, WorkerReceipt, _read_text, read_json_object
from .state import AttemptState, RunState


_AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    text: str
    path: Path | None
    warnings: tuple[str, ...]


def subtask_report_contract(receipt: Path, token: str) -> str:
    """仅 include_report 启用时加入管理者和 worker 的交接指令。"""
    return f"""完整报告模式已开启。除 result.md 外，在本轮目录原子写入 subtask-reports.json：
{{"schema_version":1,"receipt_token":"{token}","subtasks":[]}}
清单路径：`{receipt.with_name('subtask-reports.json')}`。
没有内部子任务也必须写空 subtasks，明确声明没有遗漏。若委派内部子任务，在委派前登记，
并持续更新所有层级的子任务；父任务先于子任务登记，每项格式：
{{"id":"task-1","parent_id":null,"agent":"codex","title":"具体任务",\
"status":"completed","report_file":"subtasks/task-1.md"}}
id 在本轮唯一；parent_id 为本轮父子任务 id，直接子任务为 null；agent 为 claude 或 codex。
status 为 running/completed/blocked/failed；这是子任务自报状态，不是 DSH 验收结论。
report_file 必须是本轮 subtasks/ 下唯一的相对路径，保存该子任务完整 UTF-8 报告正文，
包括结论、实现、检查和未完成事项，不得用父任务摘要或路径代替原文。所有后代都在同一清单登记。
主 result.md 保存本轮完整回答，子报告只在清单引用，避免重复粘贴；普通长检查日志可以保留为附件。
先保存所有报告和最终清单，再提交 receipt。阻塞时保留已有正文并如实标记未完成状态。
这些报告由 SDK 原文合并，不能凭报告存在或清单状态宣告验收通过。"""


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


def _subtasks(attempt: AttemptState, budget: Budget, parts: list[str], warnings: list[str]) -> None:
    root = attempt.result_file.parent
    manifest = read_json_object(_within(root / "subtask-reports.json", root), budget=budget)
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise RecordError("不支持的子任务报告清单版本")
    if manifest.get("receipt_token") != attempt.token:
        raise RecordError("子任务报告清单不属于本轮交接")
    items = manifest.get("subtasks")
    if not isinstance(items, list):
        raise RecordError("subtasks 必须是数组")
    ids: set[str] = set()
    paths: set[Path] = set()
    for index, item in enumerate(items, 1):
        budget.check()
        label = f"第 {attempt.number} 轮子任务清单第 {index} 项"
        try:
            if not isinstance(item, dict):
                raise RecordError("子任务记录必须是对象")
            task_id = item.get("id")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
                raise RecordError("子任务 id 非法")
            if task_id in ids:
                raise RecordError("重复子任务 id")
            parent = item.get("parent_id")
            if parent is not None and (not isinstance(parent, str) or parent not in ids):
                raise RecordError("parent_id 必须引用此前登记的父任务")
            ids.add(task_id)
            agent, title, status = item.get("agent"), item.get("title"), item.get("status")
            if agent not in ("claude", "codex") or status not in ("running", "completed", "blocked", "failed"):
                raise RecordError("子任务 agent/status 非法")
            if not isinstance(title, str) or not title.strip():
                raise RecordError("子任务标题为空")
            raw_path = item.get("report_file")
            if not isinstance(raw_path, str):
                raise RecordError("子任务 report_file 必须是相对路径")
            relative = Path(raw_path)
            if relative.is_absolute() or len(relative.parts) < 2 or relative.parts[0] != "subtasks" or ".." in relative.parts:
                raise RecordError("子任务报告必须位于本轮 subtasks/ 下")
            path = _within(root / relative, root)
            if not path.is_relative_to(root.resolve() / "subtasks"):
                raise RecordError("子任务报告链接越出 subtasks/ 目录")
            if path in paths:
                raise RecordError("多个子任务引用同一报告文件")
            paths.add(path)
            parts.append(f"\n### 子任务 {task_id} — {_AGENT_NAMES[agent]}：{title}\n\n")
            if status != "completed":
                warnings.append(f"{label}（{task_id}）状态为 {status}，可能未完成。")
            parts.append(_body(path, root, budget))
            parts.append("\n")
        except (OSError, ValueError, RecordError) as exc:
            message = f"{label}无法完整收集：{exc}"
            warnings.append(message)
            parts.append(f"\n> {message}\n")


def _verification(verdict: Verdict | None, failure: str | None) -> str:
    if verdict is None:
        return "\n## 二、任务验收报告\n\n未形成可返回的有效验收结论。\n\n" + (f"运行错误：{failure}\n" if failure else "")
    parts = ["\n## 二、任务验收报告\n\n",
             f"最终结论：{'通过' if verdict.status == 'accepted' else '未通过'}（{verdict.status}）\n\n",
             f"{verdict.summary}\n"]
    if verdict.remaining_issues:
        parts.append("\n### 剩余问题\n\n" + "\n".join(f"- {issue}" for issue in verdict.remaining_issues) + "\n")
    if failure:
        parts.append(f"\n运行错误：{failure}（不改写上述已校验的业务结论）\n")
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
    """文件问题以警告展示；正常运行仍传播超时/取消，错误收尾仅尽力收集。"""
    warnings: list[str] = []
    parts = ["## 一、所有任务结果报告\n\n"]
    try:
        budget.check()
        if plan is None:
            plan = ExecutionPlan.load(_within(state.plan_file, state.root), expected_run_id=state.run_id, budget=budget)
        selected = state.agent_state(plan.agent)
        count = verdict.attempts if verdict else 0
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
                    warnings.append("实际登记轮次与 verdict.attempts 不一致，报告包含已确认登记的全部轮次。")
                count = max(count, number)
            except (OSError, ValueError, RecordError) as exc:
                warnings.append(f"无法核对实际执行轮次：{exc}")
        elif verdict is None:
            warnings.append("没有实际执行轮次记录，无法确认需要收集哪些报告。")
        if count == 0:
            parts.append("没有已确认执行的 worker 轮次。\n")
        for attempt in selected.attempts[:count]:
            budget.check()
            historical = "（历史轮次）" if attempt.number < count else ""
            parts.append(f"\n### {_AGENT_NAMES[plan.agent.value]} 第 {attempt.number} 轮报告{historical}\n\n")
            try:
                _within(attempt.result_file.parent, state.root)
                _within(attempt.receipt_file, attempt.result_file.parent)
                _within(attempt.result_file, attempt.result_file.parent)
                WorkerReceipt.load(attempt.receipt_file, expected_token=attempt.token, budget=budget)
            except (OSError, ValueError, RecordError) as exc:
                warnings.append(f"第 {attempt.number} 轮未确认有效交接，报告可能不完整：{exc}")
            try:
                _within(attempt.result_file.parent, state.root)
                parts.append(_body(attempt.result_file, attempt.result_file.parent, budget))
                parts.append("\n")
            except (OSError, ValueError, RecordError) as exc:
                message = f"第 {attempt.number} 轮报告缺失或不可读：{exc}"
                warnings.append(message)
                parts.append(f"> {message}\n")
            try:
                _within(attempt.result_file.parent, state.root)
                _subtasks(attempt, budget, parts, warnings)
            except (OSError, ValueError, RecordError) as exc:
                warnings.append(f"第 {attempt.number} 轮子任务清单不可用，无法确认子报告是否齐全：{exc}")
    except OperationError as exc:
        if not partial:
            raise
        warnings.append(f"剩余预算不足或操作已停止，报告收集未完成：{exc}")
    except (OSError, ValueError, RecordError) as exc:
        warnings.append(f"报告收集未完成：{exc}")
    if warnings:
        parts.append("\n### 报告收集说明\n\n" + "\n".join(f"- {message}" for message in warnings) + "\n")
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
