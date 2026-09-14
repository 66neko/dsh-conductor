"""为一个不可变运行目录生成 DSH 管理者契约。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .state import RunState


def _command(parts: list[str]) -> str:
    return shlex.join(parts)


def build_prompt(
    state: RunState,
    *,
    skill_script: Path,
    max_attempts: int,
    attempt_timeout_seconds: int,
    keep_session: bool,
) -> str:
    # 命令由 conductor 预先完整生成，避免 DSH 在关键路径上自行拼接路径或 token。
    first = state.attempts[0]
    run_command = _command(
        [
            "python3.13",
            str(skill_script),
            "run",
            "--workspace",
            str(state.workspace),
            "--session",
            state.session,
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
    close_command = _command(
        ["python3.13", str(skill_script), "close", "--session", state.session]
    )
    verdict_example = {
        "schema_version": 1,
        "run_id": state.run_id,
        "status": "accepted",
        "agent": state.agent.value,
        "attempts": 1,
        "artifacts": ["relative/path"],
        "checks": [
            {
                "criterion": "one concrete acceptance criterion",
                "method": "command or direct inspection performed by DSH",
                "evidence": "specific observed output or file content",
                "passed": True,
            }
        ],
        "summary": "concise verdict",
        "remaining_issues": [],
    }
    retry_blocks: list[str] = []
    for attempt in state.attempts[1:]:
        send_command = _command(
            [
                "python3.13",
                str(skill_script),
                "send",
                "--session",
                state.session,
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
        retry_blocks.append(f"Attempt {attempt.number}:\n\n```bash\n{send_command}\n```")
    retry_commands = "\n\n".join(retry_blocks) or "No retry attempts are available."
    keep_instruction = (
        "Keep the tmux session after writing the verdict."
        if keep_session
        else f"After the verdict is durable, close the worker session with `{close_command}`."
    )
    return f"""You are the manager and independent verifier for one coding task.

Run identity and all paths are recorded in `{state.request_file}`. Read that JSON file, the task
file, and the acceptance file before acting. The immutable run id is `{state.run_id}` and the
workspace is `{state.workspace}`.

Load the `{state.agent.skill_name}` skill now. Use only that skill to control the worker. Start the
first worker turn with this exact command:

```bash
{run_command}
```

The worker command succeeds only after a token-bound receipt appears. That receipt means the worker
has stopped editing and is ready for inspection. It is a handoff signal, not evidence that the task
is correct. Never accept the task from the receipt, terminal text, or the worker's own claims.

After every worker turn, independently inspect the workspace. Read the produced files yourself and
run the commands needed to test every acceptance criterion. Record concrete observations, including
command results and relevant content. A check copied from the worker is invalid. Do not modify the
deliverables yourself.

When a check fails and another attempt remains, write a focused correction request to that attempt's
task file. Include the failed criterion, your observed evidence, and the required correction. Submit
it to the same tmux session with the corresponding exact command below:

{retry_commands}

Re-run all relevant verification after each attempt. Use at most {max_attempts} attempts.

Write a verdict whether the task passes, fails, is blocked, or the worker command fails. The verdict
path is `{state.verdict_file}`. First write `{state.verdict_file}.tmp`, parse it with Python 3.13 to
confirm it is valid JSON, then rename it atomically to the verdict path. Use this exact schema:

```json
{json.dumps(verdict_example, ensure_ascii=False, indent=2)}
```

Set `status` to `accepted` only when every required check passed from your own inspection. For a
rejected verdict, include failed checks and non-empty `remaining_issues`. Artifact paths must be
relative to the workspace and must identify files you personally confirmed. `attempts` is the number
of worker turns actually submitted.

{keep_instruction}
Finish with a short summary after the verdict file exists.
"""
