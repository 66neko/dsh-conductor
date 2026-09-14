"""为一个不可变运行目录生成 DSH 管理者契约。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Mapping, Set

from .models import AgentKind
from .state import AgentState, RunState


def _command(parts: list[str]) -> str:
    return shlex.join(parts)


def _agent_commands(
    state: RunState,
    agent: AgentState,
    *,
    skill_script: Path,
    attempt_timeout_seconds: int,
    available: bool,
) -> str:
    # 命令由 conductor 预先完整生成，避免 DSH 在关键路径上自行拼接路径或 token。
    first = agent.attempts[0]
    run_command = _command(
        [
            "python3.13",
            str(skill_script),
            "run",
            "--workspace",
            str(state.workspace),
            "--session",
            agent.session,
            "--task-file",
            str(first.task_file),
            "--receipt",
            str(first.receipt_file),
            "--token",
            first.token,
            "--timeout-seconds",
            str(attempt_timeout_seconds),
        ]
    )
    retry_commands: list[str] = []
    for attempt in agent.attempts[1:]:
        command = _command(
            [
                "python3.13",
                str(skill_script),
                "send",
                "--session",
                agent.session,
                "--task-file",
                str(attempt.task_file),
                "--receipt",
                str(attempt.receipt_file),
                "--token",
                attempt.token,
                "--timeout-seconds",
                str(attempt_timeout_seconds),
            ]
        )
        retry_commands.append(
            f"第 {attempt.number} 轮任务文件：`{attempt.task_file}`\n\n```bash\n{command}\n```"
        )
    close_command = _command(
        ["python3.13", str(skill_script), "close", "--session", agent.session]
    )
    retries = "\n\n".join(retry_commands) or "没有可用的返工轮次。"
    return f"""### {agent.kind.value}

- 可执行文件状态：{"可用" if available else "当前环境中不可用"}
- 必须加载的 skill：`{agent.kind.skill_name}`
- 首轮任务文件：`{first.task_file}`
- tmux 会话：`{agent.session}`

首轮精确命令：

```bash
{run_command}
```

返工精确命令：

{retries}

关闭会话精确命令：

```bash
{close_command}
```
"""


def build_prompt(
    state: RunState,
    *,
    skill_scripts: Mapping[AgentKind, Path],
    available_agents: Set[AgentKind],
    max_attempts: int,
    attempt_timeout_seconds: int,
    keep_session: bool,
) -> str:
    plan_example = {
        "schema_version": 1,
        "run_id": state.run_id,
        "agent": "codex",
        "agent_reason": "用户明确要求使用 Codex，且当前环境可用",
        "task_summary": "从用户 prompt 中提取的具体任务",
        "implementation_steps": ["步骤一", "步骤二"],
        "acceptance_criteria": [
            {"id": "criterion-1", "description": "可独立验证的验收项"}
        ],
    }
    verdict_example = {
        "schema_version": 2,
        "run_id": state.run_id,
        "status": "accepted",
        "agent": "codex",
        "attempts": 1,
        "artifacts": ["relative/path"],
        "checks": [
            {
                "criterion_id": "criterion-1",
                "criterion": "与 plan 中 criterion-1 对应的验收项",
                "method": "DSH 亲自执行的命令或直接检查",
                "evidence": "具体观察结果、输出或文件内容",
                "passed": True,
            }
        ],
        "summary": "DSH 对任务完成与验收结果的最终总结",
        "remaining_issues": [],
    }
    command_sections = "\n".join(
        _agent_commands(
            state,
            state.agent_state(kind),
            skill_script=skill_scripts[kind],
            attempt_timeout_seconds=attempt_timeout_seconds,
            available=kind in available_agents,
        )
        for kind in AgentKind
    )
    availability = ", ".join(kind.value for kind in AgentKind if kind in available_agents) or "无"
    close_instruction = (
        "写入 verdict 后保留所选 worker 的 tmux 会话。"
        if keep_session
        else "verdict 原子落盘后，使用所选 agent 对应的精确 close 命令关闭 tmux 会话。"
    )
    return f"""你是本次编码任务的唯一管理者、任务拆解者和独立验收者。

本次运行身份记录在 `{state.request_file}`，用户的唯一输入 prompt 位于
`{state.user_prompt_file}`，工作区是 `{state.workspace}`，不可变 run id 是 `{state.run_id}`。
先读取 request 与用户 prompt，再执行以下流程。当前环境可用的 worker 是：{availability}。

## 1. 拆解 prompt 并选择 worker

从用户 prompt 中提取实际任务、实施步骤和所有验收要求。若用户明确指定 Claude Code 或
Codex，必须服从该选择；若未明确指定，则根据任务特征和当前可用 worker 自行选择。一次运行
只能选择一个 agent，所有返工必须继续使用同一 tmux 会话，不能在中途切换 agent。

把拆解结果原子写入 `{state.plan_file}`：先写 `{state.plan_file}.tmp`，使用 Python 3.13
解析确认是合法 JSON，再 rename 到正式路径。plan 必须符合以下 schema；验收项 ID 必须唯一且
稳定，后续 verdict 必须逐项引用：

```json
{json.dumps(plan_example, ensure_ascii=False, indent=2)}
```

如果用户明确要求的 agent 在当前环境不可用，仍要写 plan，但不要启动其他 agent；直接进行
第 4 步并写 rejected verdict，`attempts` 为 0，每个验收项写失败 check 并说明环境证据。

## 2. 委派与监督

根据 plan 中选定的 agent，只加载对应的 skill。把任务摘要、实施步骤、必要上下文和预期结果
写入该 agent 第 1 轮的 task 文件，然后执行下方对应的精确 run 命令。不得自行修改工作区产物。

worker 命令只有在收到 token-bound receipt 后才成功返回。receipt 只证明 worker 已交回控制权，
不证明任务正确。不得从终端文字、worker 自述或屏幕稳定状态推断完成或成功。

{command_sections}

## 3. 独立验收与返工

每轮 receipt 到达后，必须由你亲自读取工作区产物，并运行足以验证 plan 中每一个验收项的命令。
记录具体方法与证据，不能复制 worker 的自述作为证据。若验收失败且还有轮次，把失败的验收项
ID、你的观察证据和明确修正要求写入所选 agent 的下一轮 task 文件，再执行对应精确 send 命令。
每轮返工后重新验证相关验收项，最多提交 {max_attempts} 轮。

## 4. 最终 verdict

无论成功、失败、环境不可用、worker 阻塞或命令失败，都必须写 verdict。先写
`{state.verdict_file}.tmp`，用 Python 3.13 解析确认合法，再原子 rename 到
`{state.verdict_file}`。schema 如下：

```json
{json.dumps(verdict_example, ensure_ascii=False, indent=2)}
```

verdict 的 `agent` 必须与 plan 一致；`checks` 必须且只能覆盖 plan 中全部验收项 ID。只有你独立
验证所有 check 均通过时才能写 accepted。rejected 必须包含非空 `remaining_issues`。
`artifacts` 只能使用工作区内的相对路径，`attempts` 是实际提交给 worker 的轮数。

{close_instruction}
最后输出一段简短总结，但调用方只以 plan、receipt、DSH 协议完成事件和 verdict 文件为事实。
"""
