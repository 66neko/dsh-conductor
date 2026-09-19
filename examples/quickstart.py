#!/usr/bin/env python3.13
"""用订单汇总任务验证完整结果交接和 tmux 历史采集，保留全部运行记录。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# 从 IDE 或直接执行本文件时也使用当前仓库源码，避免误测已安装的旧版本。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import Conductor, ConductorConfig, ConductorError, TaskResult
from conductor.tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxSession
from conductor.worker_log import WorkerLogFollower


EXPECTED_SUMMARY = {
    "paid_orders": 4,
    "total_amount": "66.00",
    "regions": {"east": "30.00", "north": "0.50", "west": "35.50"},
}
REPORT_ROWS = [f"ROW-{number:05d}" for number in range(1, 6001)]


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

        current = session.capture().splitlines()
        recent = session.capture(history_lines=DEFAULT_CAPTURE_HISTORY_LINES).splitlines()
        full = session.capture(history_lines=HISTORY_LIMIT).splitlines()
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


def task_prompt(agent: str, *, stress_report: bool = False) -> str:
    worker = {"claude": "Claude Code", "codex": "Codex"}[agent]
    report_requirements = "完整记录实际实现、执行过的检查及结果、相关文件路径和未完成事项。"
    report_checks = "确认实现说明和检查记录完整，文件末尾标记完整。"
    if stress_report:
        report_requirements += (
            "另外用 Python 在 result.md 中生成 ROW-00001 到 ROW-06000 的连续 6000 行，"
            "每行只有对应编号；不能用省略号或仅引用其他文件。"
        )
        report_checks += "检查所有编号按顺序出现且无重复或遗漏。"
    return f"""
请使用 {worker} 在当前工作区实现一个 Python 3.13 订单汇总工具，仅使用标准库。
工作区已经提供 orders.csv，禁止修改这份输入数据。
tmux-probe.log 是调用方已独立验证的采集自检日志，本任务无需读取或复述它。

实施要求：
1. 创建 sales_report.py，支持 --input CSV路径 --output JSON路径。
   CSV 必须有 order_id、region、amount、status 四列；使用 Decimal 精确处理金额，
   amount 必须是有限的非负数。只汇总 status=paid 的订单，忽略 cancelled 和 refunded。
2. 输出 JSON 包含 paid_orders、total_amount、regions 三个字段，所有金额是两位小数字符串。
   正常退出码为 0；stdout 只能输出一个与文件内容一致的 JSON 对象。
   缺失列、无效金额等输入错误必须非零退出，错误写 stderr，不能创建或覆盖输出文件。
3. 使用 unittest 编写 tests/test_sales_report.py，至少覆盖当前样本、表头完整但无数据、
   cancelled/refunded 排除、缺失列、无效金额这五种情况。不要依赖第三方库。
4. 运行测试，将完整测试输出保存到 reports/tests.log，并用 orders.csv 生成 summary.json。
5. 本轮交接 result.md 应{report_requirements}
   result.md 最后一行必须是 END-OF-RESULT。完整报告直接写文件，终端只显示简短进度。
   最终报告原子落盘后，再按控制器的交接指令写绑定 token 的 receipt。

验收标准：
- 管理者亲自运行 python3.13 -m unittest discover -s tests -v，确认上述场景通过。
- 管理者亲自执行汇总命令，读取输出文件并验证内容恰好为：
  {json.dumps(EXPECTED_SUMMARY, ensure_ascii=False)}
- 管理者另造 amount=oops 的输入验证非零退出、stderr 有错误、既有输出文件内容不变。
- 管理者从本轮预定的 result.md 读取完整报告，{report_checks}
  长文件分段读完，不能以 tmux 摘要代替文件内容。
- 屏幕输出只用于观察进度，完成事实仍以绑定 token 的 receipt 和独立验收为准。
"""


def check_worker_result(result: TaskResult, *, stress_report: bool = False) -> None:
    """调用方再检查报告和真实产物，长报告压力检查按需启用。"""
    require(result.accepted, f"DSH 未通过验收：{result.verdict.summary}")
    require(result.worker_result is not None, "SDK 没有返回 worker_result 路径")
    content = result.worker_result.read_text(encoding="utf-8")
    require(content.rstrip().endswith("END-OF-RESULT"), "结果报告末尾标记缺失")
    if stress_report:
        rows = [line for line in content.splitlines() if line.startswith("ROW-")]
        require(rows == REPORT_ROWS, "结果报告的 6000 行编号存在缺失、重复或顺序错误")
        print("[通过] 从 result.md 读到完整的 6000 行编号及结束标记。", flush=True)
    else:
        print("[通过] 已从 result.md 读取业务报告，结束标记完整。", flush=True)

    workspace = result.workspace
    output = workspace / "caller-summary.json"
    completed = subprocess.run(
        [sys.executable, "sales_report.py", "--input", "orders.csv", "--output", str(output)],
        cwd=workspace, capture_output=True, text=True, timeout=30,
    )
    require(completed.returncode == 0, f"汇总命令失败：{completed.stderr}")
    require(json.loads(completed.stdout) == EXPECTED_SUMMARY, "汇总 stdout 内容不正确")
    require(json.loads(output.read_text(encoding="utf-8")) == EXPECTED_SUMMARY, "汇总文件内容不正确")

    invalid = workspace / "caller-invalid.csv"
    invalid.write_text("order_id,region,amount,status\nBAD,east,oops,paid\n", encoding="utf-8")
    before = output.read_bytes()
    rejected = subprocess.run(
        [sys.executable, "sales_report.py", "--input", str(invalid), "--output", str(output)],
        cwd=workspace, capture_output=True, text=True, timeout=30,
    )
    require(rejected.returncode != 0 and bool(rejected.stderr.strip()), "无效输入未被正确拒绝")
    require(output.read_bytes() == before, "无效输入覆盖了既有输出")
    print("[通过] 调用方复查汇总为 66.00，无效输入被拒绝且未覆盖原结果。", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude", help="选择预设 prompt 中的 worker（默认 claude）")
    parser.add_argument("--tmux-only", action="store_true", help="仅自检 tmux，不调用 DSH 或模型")
    parser.add_argument("--stress-report", action="store_true", help="额外生成并验收 6000 行报告（增加模型上下文开销）")
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
        print("worker 屏幕日志每 10 秒采样一次，结束时立即补采；完整长文从 result.md 读取。", flush=True)
        print(f"监督阈值：{args.worker_idle_timeout_seconds} 秒；全程最多恢复 5 次；总时限 3600 秒。", flush=True)
        if args.sdk_heartbeat_counts_as_activity:
            print("SDK 心跳计入活动：持续心跳不会触发静默超时，DSH 仍会周期检查屏幕和文件。", flush=True)
        if args.stress_report:
            print("已启用 6000 行报告压力测试。", flush=True)
    try:
        check_tmux(workspace)
        if args.tmux_only:
            return 0
        (workspace / "orders.csv").write_text(
            "order_id,region,amount,status\n"
            "O001,east,19.90,paid\n"
            "O002,west,35.50,paid\n"
            "O003,east,10.10,paid\n"
            "O004,west,99.00,cancelled\n"
            "O005,north,0.50,paid\n"
            "O006,east,12.30,refunded\n",
            encoding="utf-8",
        )
        result = Conductor(
            workspace,
            ConductorConfig(keep_session=True, worker_idle_timeout_seconds=args.worker_idle_timeout_seconds,
                            sdk_heartbeat_counts_as_activity=args.sdk_heartbeat_counts_as_activity),
        ).run(
            task_prompt(args.agent, stress_report=args.stress_report),
            on_event=lambda event: print(event.format(), file=sys.stderr, flush=True),
        )
        result_json = workspace / "quickstart-result.json"
        result_json.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"SDK 结果：{result_json}\n完整报告：{result.worker_result}\nworker 屏幕日志：{result.worker_log}", flush=True)
        print(f"监督状态与恢复记录：{result.state_directory / 'supervision.json'}\n{result.state_directory / 'supervision.jsonl'}", flush=True)
        if result.accepted:
            print(f"观察会话：tmux attach -t {result.session}\n检查后关闭：tmux kill-session -t {result.session}", flush=True)
        check_worker_result(result, stress_report=args.stress_report)
        print("全部验证通过。工作区和 worker 会话已保留。", flush=True)
        return 0
    except ConductorError as exc:
        (workspace / "quickstart-error.json").write_text(json.dumps(exc.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"DSH 运行失败：{exc}\n运行记录：{exc.state_directory}", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
    print(f"保留现场：{workspace}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
