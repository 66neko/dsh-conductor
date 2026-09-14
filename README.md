# dsh-conductor

`dsh-conductor` 是一个零第三方依赖的 Python 3.13 SDK。它把一段自然语言 prompt 交给工作环境中的 DSH：DSH 负责拆解任务、根据 prompt 选择 Claude Code 或 Codex、通过 tmux 监督执行、独立验证产物，并返回结构化验收结果。

DSH 由运行环境提供，本项目不会安装或调用模型 API。Claude Code 与 Codex 分别由 `tmux-claude-code` 和 `tmux-codex` 两个独立 skill 驱动。

## SDK 使用：从安装到第一次运行

### 1. 准备运行环境

项目要求 Python 3.13 或更高版本。`dsh-conductor` 本身是零第三方依赖的 Python SDK，
但运行任务时还需要由运行环境提供 DSH、`tmux`，以及 prompt 选择的 worker（Claude Code
或 Codex）。SDK 不安装 DSH，也不调用模型 API。

建议为调用方项目创建虚拟环境：

```bash
cd /path/to/your-project
python3.13 -m venv .venv
source .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install dsh-conductor
```

安装后可以确认 SDK 版本和命令入口：

```bash
python -c 'import conductor; print(conductor.__version__)'
conductor --version
```

DSH 不在 PATH 中时，设置 `DSH_BIN`，或在 `ConductorConfig` 中传入 `dsh_bin`。首次运行前
建议执行诊断：

```bash
conductor doctor
```

`doctor` 会检查 Python、tmux、DSH、随包 skills、worker 可用性和 DSH 凭据。凭据由 DSH
管理；SDK 不会写入模型 API key。

### 2. 编写一个最小 SDK 调用

下面的程序把任务和验收标准放在同一个 prompt 中，并实时打印进度：

```python
from pathlib import Path

from conductor import Conductor, ConductorConfig, ConductorError

workspace = Path("/path/to/your-project").resolve()
client = Conductor(
    workspace,
    ConductorConfig(
        # None 表示从 DSH_BIN 或 PATH 查找 dsh。
        dsh_bin=None,
        max_attempts=2,
    ),
)

prompt = """
请使用 Codex 创建 hello.txt，内容为 Hello Conductor 加一个换行。

验收标准：hello.txt 必须存在，且 bytes 恰好等于 b'Hello Conductor\\n'。
"""

try:
    result = client.run(prompt, on_event=lambda event: print(event.format(), flush=True))
except ConductorError as exc:
    print(f"运行失败：{exc}")
    if exc.state_directory:
        print(f"运行记录：{exc.state_directory}")
else:
    print(f"accepted = {result.accepted}")
    print(f"agent = {result.plan.agent.value}")
    print(result.verdict.summary)
```

`run()` 的第一个参数是唯一业务输入。prompt 中可以明确写“使用 Claude Code”或“使用
Codex”；没有明确指定时，DSH 根据任务与当前环境选择。调用方不再传 `agent`、`task` 或
`verify` 参数。

`on_event` 会在运行中接收 `RunEvent`，事件来源包括 `dsh`、`claude`、`codex` 和
`conductor`。事件只用于显示进度，完成事实仍由 DSH 协议、worker receipt 和最终 verdict
文件决定。`TaskResult` 包含：

- `plan`：DSH 生成的 agent、任务摘要、步骤和带 ID 的验收项；
- `verdict`：DSH 独立检查后的 `accepted`/`rejected`、每项证据、产物和剩余问题；
- `state_directory`：本轮 request、plan、task、receipt、verdict 与 `worker-screen.log`；
- `dsh`：协议状态、耗时、事件数量和 DSH 最后文本。

被 DSH 拒绝是正常业务结果，`result.accepted` 为 `False`；DSH 启动失败、协议失败或结果文件不合法时抛出 `ConductorError`。

### 3. 处理结果并读取 JSON

业务代码通常先检查 `result.accepted`，再读取结构化计划和验收结论：

```python
payload = result.to_json()

if payload["status"] == "accepted":
    for artifact in payload["verdict"]["artifacts"]:
        print("已验收产物：", artifact)
else:
    print("未通过：", payload["verdict"]["remaining_issues"])
```

状态目录默认创建在 `<workspace>/.dsh-conductor/`，每次运行使用独立的 run id。若需要将
审计记录保存到其他位置，可传入 `ConductorConfig(state_dir=Path("/path/to/state"))`。
目录包含原始 prompt、DSH 计划、worker task/receipt、verdict 和可选的 tmux 屏幕日志，
建议将 `.dsh/` 与 `.dsh-conductor/` 加入项目的 Git 忽略规则。

### 4. 可直接运行的示例

下例以 `run_task.py` 在已激活的虚拟环境中运行时为例：

```python
import json
from pathlib import Path

from conductor import Conductor

result = Conductor(Path.cwd()).run(
    "请使用 Claude Code 创建 hello.txt。验收标准：文件存在且内容为 Hello。",
)
print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
raise SystemExit(0 if result.accepted else 1)
```

更完整的字段说明、错误 JSON 和实时事件格式见 [`docs/result-json.md`](docs/result-json.md)。
可运行示例见 [`examples/quickstart.py`](examples/quickstart.py)。

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

`.dsh/skills/` 和 `.dsh-conductor/` 都是 SDK 生成的运行目录，建议加入项目的 Git 忽略规则。

## 日志与状态

conductor 会同时轮询两个候选 tmux 会话；DSH 选择哪个 agent 后，只有实际存在的会话产生屏幕日志。默认每 5 秒采样一次，SDK 可通过 `ConductorConfig(worker_log_interval_seconds=...)` 调整，CLI 可通过 `--worker-log-interval-seconds` 调整。变化会通过 `RunEvent(source="claude"/"codex", kind="worker_output")` 回调，并追加到 `worker-screen.log`。屏幕文字不会被当作完成或验收信号。

状态目录默认位于当前 `workspace` 下的 `.dsh-conductor/`；可通过
`ConductorConfig(state_dir=...)` 或 CLI 的 `--state-dir` 指定其他位置：

```text
<workspace>/.dsh-conductor/runs/<run-id>/
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

推送 GitHub Release 后，`.github/workflows/publish.yml` 会先在独立 job 中运行测试并构建
发行包，再用 PyPI Trusted Publishing 上传已验证的 wheel 与 sdist。首次发布前，需要在
PyPI 为 GitHub Actions 配置 Trusted Publisher，并在 GitHub 创建 `pypi` 环境。完整字段、
版本标签规则和发布步骤见 [`docs/pypi-publishing.md`](docs/pypi-publishing.md)。
