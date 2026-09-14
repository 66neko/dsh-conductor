# dsh-conductor

`dsh-conductor` 是一个零第三方依赖的 Python 3.13 SDK。它把一段自然语言 prompt 交给工作环境中的 DSH：DSH 负责拆解任务、根据 prompt 选择 Claude Code 或 Codex、通过 tmux 监督执行、独立验证产物，并返回结构化验收结果。

DSH 由运行环境提供，本项目不会安装或调用模型 API。Claude Code 与 Codex 分别由 `tmux-claude-code` 和 `tmux-codex` 两个独立 skill 驱动。

## SDK 使用

```python
from pathlib import Path

from conductor import Conductor, ConductorConfig

client = Conductor(
    workspace=Path("/path/to/project"),
    config=ConductorConfig(
        # dsh_bin=None 时从 DSH_BIN 或 PATH 查找。
        dsh_bin=None,
        max_attempts=2,
    ),
)

result = client.run(
    """
    请使用 Codex 完成以下任务：创建 hello.txt，内容为 Hello Conductor 加一个换行。

    验收标准：hello.txt 必须存在，且 bytes 恰好等于 b'Hello Conductor\\n'。
    """,
    on_event=lambda event: print(event.format()),
)

print(result.accepted)
print(result.plan.task_summary)
print(result.verdict.summary)
```

`run()` 的第一个参数是唯一业务输入。prompt 中可以明确写“使用 Claude Code”或“使用 Codex”；没有明确指定时，DSH 根据任务与当前环境选择。调用方不再传 `agent`、`task` 或 `verify` 参数。

`on_event` 会在运行中接收 `RunEvent`，事件来源包括 `dsh`、`claude`、`codex` 和 `conductor`。事件只用于显示进度，完成事实仍由 DSH 协议、worker receipt 和最终 verdict 文件决定。`TaskResult` 包含：

- `plan`：DSH 生成的 agent、任务摘要、步骤和带 ID 的验收项；
- `verdict`：DSH 独立检查后的 `accepted`/`rejected`、每项证据、产物和剩余问题；
- `state_directory`：本轮 request、plan、task、receipt、verdict 与 `worker-screen.log`；
- `dsh`：协议状态、耗时、事件数量和 DSH 最后文本。

被 DSH 拒绝是正常业务结果，`result.accepted` 为 `False`；DSH 启动失败、协议失败或结果文件不合法时抛出 `ConductorError`。

完整字段、嵌套对象、错误结果和实时事件格式见 [`docs/result-json.md`](docs/result-json.md)。可运行示例见 [`examples/quickstart.py`](examples/quickstart.py)。

## CLI

CLI 是 SDK 的薄封装，保留给脚本和人工调用：

```bash
python3.13 -m conductor install-skills --workspace /path/to/project
python3.13 -m conductor doctor
python3.13 -m conductor run \
  --workspace /path/to/project \
  --prompt '请使用 Claude Code 创建 hello.txt。验收标准：文件存在且内容为 Hello。'
```

`run` 的 stdout 始终只有一个 JSON 对象；实时事件由 CLI 写入 stderr，因此可以安全地重定向 stdout。每次运行启动 DSH 前，SDK 都会把包内两个 skill 直接覆盖到 `<workspace>/.dsh/skills/`，供 DSH 项目级发现；不会写入 `~/.dsh/skills`，任务结束后也不会删除。`install-skills --workspace` 可提前执行同样的复制操作。`show` 可读取最近一次运行的 request、用户 prompt、manager prompt、plan、verdict 和日志路径。

`.dsh/skills/` 是 SDK 生成的运行目录，建议加入项目的 Git 忽略规则。

## 日志与状态

conductor 会同时轮询两个候选 tmux 会话；DSH 选择哪个 agent 后，只有实际存在的会话产生屏幕日志。默认每 5 秒采样一次，SDK 可通过 `ConductorConfig(worker_log_interval_seconds=...)` 调整，CLI 可通过 `--worker-log-interval-seconds` 调整。变化会通过 `RunEvent(source="claude"/"codex", kind="worker_output")` 回调，并追加到 `worker-screen.log`。屏幕文字不会被当作完成或验收信号。

状态目录默认是 `$XDG_STATE_HOME/dsh-conductor`，未设置时为 `~/.local/state/dsh-conductor`：

```text
runs/<run-id>/
├── request.json
├── user-prompt.md
├── manager-prompt.md
├── plan.json
├── verdict.json
├── worker-screen.log
└── attempts/
    ├── claude/1/{task.md,receipt.json}
    └── codex/1/{task.md,receipt.json}
```

每轮运行使用新的 run id 和 receipt token，避免读取旧结论。accepted verdict 必须同时满足 DSH `turn/end` + `session.status=idle`、所有验收项通过、产物路径安全且选定 agent 的最终 receipt 有效。

## 开发验证

```bash
python3.13 -m compileall -q conductor skills tests
python3.13 -m unittest discover -v
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-claude-code
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-codex
```

## 发布

推送 GitHub Release 后，`.github/workflows/publish.yml` 会使用 PyPI Trusted Publishing
自动构建并发布 wheel 与 sdist。首次发布前，需要在 PyPI 为项目配置 GitHub Actions 的
Trusted Publisher，仓库名为 `66neko/dsh-conductor`，工作流为 `publish.yml`，环境名为
`pypi`。
