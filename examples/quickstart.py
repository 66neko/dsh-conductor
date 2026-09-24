#!/usr/bin/env python3.13
"""用 Hello, DSH! 小任务体验结果交接与简明报告，保留全部运行记录。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TextIO

# 从 IDE 或直接执行本文件时也使用当前仓库源码，避免误测已安装的旧版本。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import Conductor, ConductorConfig, ConductorError, TaskResult
from conductor.tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxSession
from conductor.worker_log import WorkerLogFollower


EXPECTED_OUTPUT = b"Hello, DSH!\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_tmux(workspace: Path) -> None:
    """独立的确定性采集自检，不依赖 agent TUI 是否展示工具的完整输出。"""
    session = TmuxSession.create(
        name=f"dsh-quickstart-probe-{uuid.uuid4().hex[:12]}",
        workspace=workspace,
        agent="fixture",
        command=[
            sys.executable, "-c",
            "import sys,time; "
            "sys.stdout.write('\\n'.join(f'PROBE-{i:05d}' for i in range(1, 6001))); "
            "sys.stdout.flush(); time.sleep(60)",
        ],
    )
    try:
        deadline = time.monotonic() + 10
        while "PROBE-06000" not in session.capture():
            require(time.monotonic() < deadline, "tmux 自检输出超时")
            time.sleep(0.05)

        # tmux 3.2a 的 capture-pane -J 会保留行尾填充空格；只在自检比较时去掉，
        # 与 WorkerLogFollower 的落盘格式一致，仍严格检查行数、内容及顺序。
        current = [line.rstrip() for line in session.capture().splitlines()]
        recent = [line.rstrip() for line in session.capture(history_lines=DEFAULT_CAPTURE_HISTORY_LINES).splitlines()]
        full = [line.rstrip() for line in session.capture(history_lines=HISTORY_LIMIT).splitlines()]
        actual_limit = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session.name, "#{history_limit}"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        require(actual_limit == str(HISTORY_LIMIT), f"tmux 历史上限未生效：{actual_limit}")
        require(full == [f"PROBE-{i:05d}" for i in range(1, 6001)], "tmux 全量历史缺行或顺序错误")
        require(recent == full[-(DEFAULT_CAPTURE_HISTORY_LINES + len(current)):], "tmux 最近历史不完整")

        updates: list[tuple[str, ...]] = []
        log_file = workspace / "tmux-probe.log"
        WorkerLogFollower(
            session_name=session.name, agent="fixture", log_file=log_file,
            sink=lambda _agent, lines: updates.append(tuple(lines)),
        ).sample_once()
        persisted = [line[2:] for line in log_file.read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
        require(persisted == recent, "tmux 日志没有保存全部采样行")
        require(len(updates) == 1 and len(updates[0]) == 12, "实时展示行数异常")
        print(
            f"[通过] tmux：当前屏幕 {len(current)} 行，最近历史连同屏幕 {len(recent)} 行，"
            f"扩大读取 {len(full)} 行，实际历史上限 {actual_limit} 行。",
            flush=True,
        )
        print(f"[通过] 日志保存 {len(persisted)} 行，实时回调 12 行：{log_file}", flush=True)
    finally:
        session.close()


def task_prompt(agent: str) -> str:
    worker = {"claude": "Claude Code", "codex": "Codex"}[agent]
    return f"""
请使用 {worker} 在当前工作区完成一个简短的 Python 3.13 任务，仅使用标准库。

实施要求：
1. 创建 hello.py，执行 python3.13 hello.py 时，stdout 恰好为 Hello, DSH! 后跟一个换行，
   退出码为 0，stderr 为空。直接运行脚本检查结果，无需编写单独的测试文件。
2. 本轮交接 result.md 用简短文字记录实现、实际执行的检查及结果、文件路径和未完成事项。
   result.md 最后一行必须是 END-OF-RESULT。报告直接写文件，终端只显示简短进度。
   最终报告原子落盘后，再按控制器的交接指令写绑定 token 的 receipt。

验收标准：
- 管理者亲自执行 python3.13 hello.py，确认 stdout 恰好为 Hello, DSH! 后跟一个换行，
  退出码为 0，stderr 为空。
- 管理者读取本轮预定的 result.md，确认有实现与检查结果，最后一行为 END-OF-RESULT。
- 屏幕输出只用于观察进度，完成事实仍以绑定 token 的 receipt 和独立验收为准。
"""


def check_worker_result(result: TaskResult) -> None:
    """调用方独立运行真实产物，并确认结果报告完整交接。"""
    require(result.accepted, f"DSH 未通过验收：{result.verdict.summary}")
    require(result.worker_result is not None, "SDK 没有返回 worker_result 路径")
    content = result.worker_result.read_text(encoding="utf-8")
    lines = content.rstrip().splitlines()
    require(bool(lines) and lines[-1] == "END-OF-RESULT", "结果报告末尾标记缺失")

    completed = subprocess.run(
        [sys.executable, "hello.py"], cwd=result.workspace, capture_output=True, timeout=30,
    )
    require(completed.returncode == 0, f"hello.py 执行失败：{completed.stderr.decode('utf-8', errors='replace')}")
    require(completed.stdout == EXPECTED_OUTPUT, "hello.py 的 stdout 内容不正确")
    require(completed.stderr == b"", "hello.py 的 stderr 不为空")
    print("[通过] 调用方复查 hello.py 输出、退出码及 stderr 正确，报告结束标记完整。", flush=True)


def print_report(result: TaskResult | ConductorError, *, file: TextIO | None = None) -> None:
    """直接展示 SDK 返回的报告快照；落盘路径可能缺失，收集提示可单独存在。"""
    if result.report is not None:
        print(result.report, file=file, flush=True)
    if result.report_file is not None:
        print(f"报告文件：{result.report_file}", file=file, flush=True)
    for warning in result.report_warnings:
        print(f"报告收集提示：{warning}", file=file, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude", help="选择预设 prompt 中的 worker（默认 claude）")
    parser.add_argument("--tmux-only", action="store_true", help="仅自检 tmux，不调用 DSH 或模型")
    parser.add_argument("--include-report", action=argparse.BooleanOptionalAction, default=True,
                        help="默认展示 worker 报告与总体验收结论；用 --no-include-report 关闭")
    parser.add_argument("--worker-idle-timeout-seconds", type=int, default=300, help="活动静默阈值，默认 300 秒")
    parser.add_argument("--sdk-heartbeat-counts-as-activity", action=argparse.BooleanOptionalAction, default=True,
                        help="默认 SDK 心跳也算活动；用 --no-sdk-heartbeat-counts-as-activity 排除")
    args = parser.parse_args()
    # mkdtemp 不自动删除，运行结束后仍能查看产物、回执和日志。
    workspace = Path(tempfile.mkdtemp(prefix=f"dsh-quickstart-{args.agent}-"))
    print(f"测试工作区（运行后保留）：{workspace}", flush=True)
    if not args.tmux_only:
        print(f"中间状态目录：{workspace / '.dsh-conductor' / 'runs'}", flush=True)
        print(f"最终 JSON（run 返回后生成）：{workspace / 'quickstart-result.json'}", flush=True)
        print("worker 屏幕日志每 10 秒采样一次，结束时立即补采；任务报告从 result.md 读取。", flush=True)
        print(f"静默阈值：连续 {args.worker_idle_timeout_seconds} 秒无活动，有活动重新计时；全程最多恢复 5 次；总时限 3600 秒。", flush=True)
        print("waiting for bash 显示工具调用累计时长；watch 单次等待到期会交回 DSH 检查，不会停止 worker。", flush=True)
        if args.sdk_heartbeat_counts_as_activity:
            print("SDK 心跳计入活动：持续心跳不会触发静默超时，DSH 仍会周期检查屏幕和文件。", flush=True)
    try:
        if args.tmux_only:
            check_tmux(workspace)
            return 0
        result = Conductor(
            workspace,
            ConductorConfig(keep_session=True, worker_idle_timeout_seconds=args.worker_idle_timeout_seconds,
                            sdk_heartbeat_counts_as_activity=args.sdk_heartbeat_counts_as_activity,
                            include_report=args.include_report),
        ).run(
            task_prompt(args.agent),
            on_event=lambda event: print(event.format(), file=sys.stderr, flush=True),
        )
        result_json = workspace / "quickstart-result.json"
        result_json.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"SDK 结果：{result_json}\n完整报告：{result.worker_result}\nworker 屏幕日志：{result.worker_log}", flush=True)
        print_report(result)
        print(f"监督状态与恢复记录：{result.state_directory / 'supervision.json'}\n{result.state_directory / 'supervision.jsonl'}", flush=True)
        if result.accepted:
            print(f"观察会话：{result.attach_command}\n检查后关闭：conductor cleanup --state-directory {result.state_directory}", flush=True)
        check_worker_result(result)
        print(f"全部验证通过。工作区已保留；清理状态：{result.cleanup.status}。", flush=True)
        return 0
    except ConductorError as exc:
        (workspace / "quickstart-error.json").write_text(json.dumps(exc.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"DSH 运行失败：{exc}\n运行记录：{exc.state_directory}", file=sys.stderr)
        print_report(exc, file=sys.stderr)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
    print(f"保留现场：{workspace}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
