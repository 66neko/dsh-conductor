"""为一个不可变运行目录生成 DSH 管理者契约。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Mapping, Set

from .models import VERDICT_SCHEMA_VERSION, AgentKind
from .state import AgentState, RunState


def _command(parts: list[str]) -> str:
    return shlex.join(parts)


def _agent_commands(
    state: RunState,
    agent: AgentState,
    *,
    skill_script: Path,
    available: bool,
) -> str:
    first = agent.attempts[0]
    run_command = _command([
        "python3.13", str(skill_script), "run",
        "--request", str(state.request_file),
        "--session", agent.session,
        "--task-file", str(first.task_file),
        "--receipt", str(first.receipt_file),
        "--token", first.token,
    ])

    retry_commands: list[str] = []
    for attempt in agent.attempts[1:]:
        command = _command([
            "python3.13", str(skill_script), "send",
            "--request", str(state.request_file),
            "--session", agent.session,
            "--task-file", str(attempt.task_file),
            "--receipt", str(attempt.receipt_file),
            "--token", attempt.token,
        ])
        retry_commands.append(f"第 {attempt.number} 轮：`{attempt.task_file}` → `{attempt.result_file}`\n```bash\n{command}\n```")

    retries = "\n\n".join(retry_commands) or "没有可用的返工轮次。"
    control_names = ["watch", "recover", "choose", "stop"]
    if agent.kind == AgentKind.CODEX:
        control_names.append("reconnect")
    controls = "\n".join(
        f"- `{name}`：`{_command(['python3.13', str(skill_script), name, '--request', str(state.request_file), '--session', agent.session])}`"
        for name in control_names
    )

    return f"""### {agent.kind.value}

可用性：{"✓" if available else "✗"}  |  skill：`{agent.kind.skill_name}`  |  会话：`{agent.session}`

**首轮命令：**
```bash
{run_command}
```

**返工命令：**
{retries}

**监督命令：**
{controls}

"""


def build_prompt(
    state: RunState,
    *,
    skill_scripts: Mapping[AgentKind, Path],
    available_agents: Set[AgentKind],
    max_attempts: int,
    worker_idle_timeout_seconds: int,
    keep_session: bool,
    sdk_heartbeat_counts_as_activity: bool = True,
    include_report: bool = False,
) -> str:
    plan_example = {
        "schema_version": 1,
        "run_id": state.run_id,
        "agent": "codex",
        "agent_reason": "用户明确要求使用 Codex，且当前环境可用",
        "task_summary": "从用户 prompt 中提取的具体任务",
    }

    verdict_example = {
        "schema_version": VERDICT_SCHEMA_VERSION,
        "status": "accepted",
        "summary": "result.md 已覆盖用户 prompt 的全部目标",
        "remaining_issues": [],
    }

    command_sections = "\n".join(
        _agent_commands(state, state.agent_state(kind), skill_script=skill_scripts[kind], available=kind in available_agents)
        for kind in AgentKind
    )

    validate_command = _command([
        "python3.13", "-P", "-m", "conductor", "validate-verdict",
        "--request", str(state.request_file),
        "--verdict", f"{state.verdict_file}.tmp"
    ])

    availability = ", ".join(kind.value for kind in AgentKind if kind in available_agents) or "无"

    heartbeat_note = ("SDK 心跳计入活动。" if sdk_heartbeat_counts_as_activity
                     else "SDK 心跳不计入活动，只统计 worker 输出。")

    session_policy = ("accepted 时保留 worker 会话。" if keep_session
                     else "验收后 SDK 自动关闭会话。")

    report_mode = ""
    if include_report:
        report_mode = """
**报告模式：** 本次 include_report=true。SDK 会合并各轮 result.md 与 verdict 结论；verdict.summary 用一两句话概括。"""

    return f"""你是本次编码任务的管理者、拆解者和报告验收者。

**运行身份：** `{state.request_file}`
**用户 prompt：** `{state.user_prompt_file}`
**工作区：** `{state.workspace}`
**run id：** `{state.run_id}`
**可用 worker：** {availability}

资源上下文在 runtime.json；控制器通过 --request 自动读取私有 tmux socket 和截止时间。
不得连接默认 socket。sdk-stop.json 出现后停止所有新操作。{report_mode}

## 1. 拆解与选择

读取用户 prompt，提取任务目标。若用户明确指定 Claude Code 或 Codex，必须服从；否则根据任务特征
和可用 worker 选择。一次运行只能选择一个 agent，所有返工沿用同一会话。

plan 写入 `{state.plan_file}`（先写 .tmp，解析确认后 rename）：

```json
{json.dumps(plan_example, ensure_ascii=False, indent=2)}
```

用户要求的 agent 不可用时，仍写 plan，但不启动 worker；直接进入第 4 步写 rejected verdict。

## 2. 委派与监督

根据 plan 选定的 agent 加载对应 skill。**首轮 task 文件直接使用用户 prompt 中的任务部分（从 user_prompt_file 中提取，
去掉验收标准部分），不要改写或加工用户的原始任务描述**。返工轮在用户原始任务后追加本轮修正要求。

worker 必须在 result.md 原子落盘后才写 receipt。

run/send 只登记任务并立即返回，不代表完成。随后持续调用 watch，每次最多等待 300 秒且不超过
剩余执行时间。watch 返回 starting/submitting/running/reconnecting 时检查状态后继续 watch，不能重复 run/send。
watch 在就绪后自动提交已登记的任务。

连续 {worker_idle_timeout_seconds} 秒无活动时返回 needs_attention。任何 worker 终端字节、重复错误、
spinner、计时器、光标变化均重置静默时钟。{heartbeat_note}

根据返回的屏幕、snapshot_file、实际文件和进程状态判断：

- **网络/模型暂时失败：** 确认输入界面可用后 recover 继续；必要时 --interrupt
- **Codex 正在 Reconnecting：** 自动重试期间继续 watch，不 recover、choose 或中断；显示最后一次重试仍须等待结束
- **Codex reconnect_required：** 自动重试耗尽且未恢复，用 reconnect 继续原任务，不扣错误恢复次数
- **输入框等待选择：** 根据任务用 choose 提供精确按键及理由
- **submission_pending：** 任务在输入框，用 choose --keys Enter 补发（普通提交计入恢复预算，重连提交除外）
- **submission_unconfirmed：** 检查快照和屏幕，仅在输入框空且未忙时 recover
- **声称完成却缺文件：** recover 要求补齐；无效回执归档后由 worker 重写
- **明确失败或恢复额度耗尽：** stop 保存末屏并终止，写 rejected verdict

recover 与反复菜单共享全 run 最多 5 次恢复预算；首次普通菜单选择免费。每次操作后重新观察，
不连续堆积"继续"。Codex reconnect 及其提交补发单独计数，不受错误恢复次数限制，但仍受全 run 截止时间限制。

只有 watch 返回 receipt_ready 才代表收到 token-bound receipt。终端文字可用于诊断、恢复、选择
和失败判断，不能作为成功依据。完整内容从 result_file 读取。

{command_sections}

## 3. 报告验收与返工

本项目的验收对象只有 worker 的 result.md 报告，不检查工作区、不运行命令、不复核产物。
每轮 receipt 到达后，完整读取 result.md，只判断报告内容是否完成了用户 prompt 中的全部目标
（含 prompt 写明的验收标准）。报告缺项、只做了一部分、声明失败或未完成，即判定不通过。

不通过且还有轮次时，把报告缺少的目标写入下一轮 task 文件，执行对应 send 命令。最多提交 {max_attempts} 轮。
receipt 为 blocked 表示 worker 声明未完成，不能作为通过的一轮。

## 4. 最终 verdict（必须完成）

无论通过、失败、环境不可用或阻塞，结束前都必须写 verdict。先写 `{state.verdict_file}.tmp`，再执行：

```bash
{validate_command}
```

校验命令输出 "status": "valid" 时已自动 rename 为正式 verdict，无需再操作；输出 invalid 时按 error 修正 .tmp 后重试。

```json
{json.dumps(verdict_example, ensure_ascii=False, indent=2)}
```

- status：accepted 或 rejected；accepted 要求最后一轮 result.md 完成全部目标且 receipt 为 ready_for_verification
- summary：一两句话结论
- remaining_issues：accepted 时为 []；rejected 时列出报告未完成的目标，不能为空

run id、agent 与轮次由 SDK 从 plan 和回执推导，不要写入 verdict。
rejected 前必须 stop。{session_policy}
最后输出简短总结，但调用方只以 plan、receipt、DSH 协议完成事件和 verdict 文件为事实。
"""
