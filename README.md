# dsh-conductor

`dsh-conductor` 是一个零第三方依赖的 Python 3.13 SDK。它把一段自然语言 prompt 交给工作环境中的 DSH：DSH 负责拆解任务、根据 prompt 选择 Claude Code 或 Codex、通过 tmux 监督执行、独立验证产物，并返回结构化验收结果。

DSH 由运行环境提供，本项目不会安装或调用模型 API。Claude Code 与 Codex 分别由 `tmux-claude-code` 和 `tmux-codex` 两个独立 skill 驱动。

## SDK 使用：从安装到第一次运行

### 1. 准备运行环境

项目要求 Python 3.13 或更高版本。完整运行与恢复清理当前支持 Linux（含 WSL2），进程身份核验使用 `/proc`。`dsh-conductor` 本身是零第三方依赖的 Python SDK，
但运行任务时还需要由运行环境提供 DSH、`tmux`，以及 prompt 选择的 worker（Claude Code
或 Codex）。SDK 不安装 DSH，也不调用模型 API。

建议为调用方项目创建虚拟环境：

```bash
cd /path/to/your-project
python3.13 -m venv .venv
source .venv/bin/activate
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
- `dsh`：协议状态、耗时、事件数量和 DSH 最后文本；
- `cleanup`：资源清理状态、未回收资源和诊断；
- `tmux_socket` / `attach_command`：本轮私有 tmux socket 和观察命令。

被 DSH 拒绝是正常业务结果，`result.accepted` 为 `False`；DSH 启动失败、协议失败或结果文件不合法时抛出 `ConductorError`。

0.5.2 新增了可选的完整报告模式。0.5.3 保留全部任务报告正文，将报告末尾的验收展示精简为
统一结论；详细验收记录继续保留在结构化 `verdict` 中：

```python
result = Conductor(workspace, ConductorConfig(include_report=True)).run(prompt)
print(result.report)          # 各轮和已登记子任务的完整正文，以及统一验收结论
print(result.report_file)     # 本轮 report.md；未落盘时为 None
payload = result.to_json()   # payload["report"] 同样包含完整正文
```

SDK 和 CLI 默认关闭完整报告，CLI 加 `--include-report` 开启。quickstart 示例默认开启，
可运行 `python3.13 examples/quickstart.py --agent codex`。报告末尾只展示最终状态、总结与必要的
剩余问题；详细检查和产物仍保留在 `verdict` 中。开启后若出现缺失、未完成或无法读取的报告，
通过 `report_warnings` 明示；业务是否通过仍看 `status`/`verdict`。详见[完整报告使用指南](docs/full-report.md)。

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

### 4. 取消、总时限与清理

```python
import threading
from conductor import Conductor, ConductorConfig, ConductorError, cleanup_run

cancel = threading.Event()  # 外部 Runtime 可以随时在另一个线程调用 cancel.set()
try:
    result = Conductor(workspace, ConductorConfig(
        timeout_seconds=600, cleanup_timeout_seconds=5,
    )).run(prompt, cancel_event=cancel)
    print(result.cleanup.status)  # completed 或明确保留会话时的 retained
except ConductorError as exc:
    print(exc.code, exc.phase, exc.to_json())
    if exc.cleanup and exc.cleanup.status == "incomplete" and exc.state_directory:
        print(cleanup_run(exc.state_directory).to_json())
```

`timeout_seconds` 覆盖准备、初始化、执行、验收和清理。运行前预留
`min(cleanup_timeout_seconds, timeout_seconds * 0.1)` 给清理，例如 600 秒任务最晚在
第 595 秒进入清理。阶段 timeout 只能缩短等待，活动、watch 和返工不延长总截止时间。
这是相对 0.4.1 的行为变更；旧版只限制 DSH 管理回合。

`keep_session=True` 仅在 accepted 时保留 worker；rejected、取消、超时和执行故障都清理。
保留会话不删除任何审计文件。观察时使用 `result.attach_command`，清理时可以调用
`cleanup_run(result.state_directory)`，或运行：

```bash
conductor cleanup --state-directory /absolute/path/to/run
```

异常按 `code` 分支，不解析错误文字。原执行错误不会被清理失败覆盖；可信 verdict 已生成但
必需资源未回收时抛出 `cleanup_failed`，`exc.result` 保留已验证结果，避免误重跑任务。
`ConductorConfig` 非法值仍抛 `ValueError`；CLI 转成 `invalid_config` JSON。

SDK 不改宿主信号处理器。CLI 把 SIGINT/SIGTERM 转为取消，清理后退出；退出码依次为
accepted=0、rejected/普通错误=1、timeout=124、SIGINT=130、SIGTERM=143。
同一 workspace 的重叠运行返回 `workspace_busy`；不同工作区可以独立运行。

回调在独立线程串行执行，默认最多缓存 1024 条进度事件，积压时丢弃旧展示事件；文件证据
保留。回调应及时返回。SDK 可以停止后续派发，但不能强制中止已执行的任意 Python 回调。
完整边界、错误码和迁移说明见 [运行生命周期](docs/lifecycle.md)。
取消示例见 [examples/cancellation.py](examples/cancellation.py)。

### 5. 可直接运行的示例

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

仓库中的 quickstart 让 worker 创建 `hello.py`，运行后输出 `Hello, DSH!` 和一个换行，
并交付简短的 `result.md`。DSH 独立执行脚本验收，调用方再检查退出码、stdout 与 stderr。
默认流程直接运行这个轻量任务，便于快速验证 SDK、worker 交接和报告返回。

在仓库根目录运行（直接执行脚本会使用当前仓库源码）：

```bash
python3.13 -m conductor doctor
python3.13 examples/quickstart.py                 # 默认使用 Claude Code
python3.13 examples/quickstart.py --agent codex   # 使用 Codex 再验证一次
python3.13 examples/quickstart.py --no-include-report  # 关闭合并报告
```

示例默认启用 `include_report`，直接展示各轮 worker 和已登记子任务的报告正文，最后给出统一
验收结论。`--include-report` 可显式开启，`--no-include-report` 可关闭；SDK 的配置默认值仍为
`False`。完整返回值保存在 `quickstart-result.json`，有合并报告时一并保存内联正文，并展示
可用的 `report_file` 路径和收集提示。

tmux 长输出采集自检独立运行，不启动 DSH 或模型：

```bash
python3.13 examples/quickstart.py --tmux-only
```

自检由本地 Python 生成和检查 6000 行输出。正常会看到 tmux 当前屏幕 50 行、最近历史连同
屏幕 5050 行、扩大读取 6000 行；日志保留 5050 行，实时回调只有 12 行。
真实 worker 任务成功后显示“全部验证通过”。
每次使用新的 `/tmp/dsh-quickstart-…` 工作区（系统临时目录配置可改变位置），运行结束后保留
`quickstart-result.json`、`result.md` 和日志；成功时保留 worker 会话并打印 attach/关闭命令，失败时停止 worker。
`quickstart-result.json` 在 `run()` 返回后才写入；运行中的 request、plan、verdict 和屏幕日志
位于 `<workspace>/.dsh-conductor/runs/<run-id>/`，每轮 task、result、receipt 位于该目录的
`attempts/<claude或codex>/<轮次>/`。`<workspace>/.dsh-conductor/latest.json` 记录最近运行的目录。
运行中的 worker 屏幕日志每 10 秒采样一次，结束时立即补采；DSH 协议事件仍实时显示。
`--agent` 是此示例选择预设 prompt 的参数；SDK 业务输入仍只有 workspace 与完整 prompt。

验证仅由 worker 活动驱动的静默检测时，可以排除 SDK 自动心跳，并缩短测试阈值：

```bash
python3.13 examples/quickstart.py --agent codex \
  --no-sdk-heartbeat-counts-as-activity --worker-idle-timeout-seconds 30
```

正常运行使用默认 300 秒。示例会打印 `supervision.json` 和 `supervision.jsonl` 路径，可查看
诊断、恢复次数、选择理由与停止原因。无需真实模型的故障回归运行
`python3.13 -m unittest tests.test_supervision tests.test_tmux tests.test_sdk -v`，覆盖恢复上限、
菜单、缺失报告、迟到回执及总超时；tmux 流程使用本地模拟 worker。

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

DSH 负责监督判断；控制器把提交、观察、恢复、选择和结束拆成独立操作。`run/send` 登记任务后
立即返回，`watch` 就绪时提交任务、每 10 秒检查一次，每次最多等待 300 秒就把屏幕和文件状态
交回 DSH。DSH 不再把整轮任务挂在长时间 `job_output` 上等待。菜单、连接错误、无效回执和
worker 退出会提前交回判断；正常运行也会周期返回屏幕，便于发现“说完成了但文件没写好”。

Codex 使用 bracketed paste，先读屏确认草稿，再发送 Enter；草稿清空后才从 `submitting` 进入
`running`。多行草稿或折叠粘贴仍停在输入框时，会提前返回 `submission_pending`，由 DSH 补发
一次 Enter 并重新观察；每次补发计入全 run 最多 5 次恢复预算。30 秒仍不能确认提交则返回
`submission_unconfirmed`，要求 DSH 检查是否尚未启动。提交确认不受 SDK 心跳影响，也不等待
300 秒静默超时。任务成功仍以有效 receipt、完整结果文件和独立验收为准。

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `worker_idle_timeout_seconds` | 300 | 连续没有活动的阈值，替代旧 `attempt_timeout_seconds` |
| `sdk_heartbeat_counts_as_activity` | True | SDK waiting 心跳也重置活动时钟 |
| `max_recovery_attempts` | 5 | 全 run 主动恢复上限，可设 1–5 |
| `max_attempts` | 2 | 业务委派总轮数（含首轮），与恢复次数分开 |
| `timeout_seconds` | 3600 | 整个 run 的总预算，包含准备、初始化、执行、验收和清理 |
| `cleanup_timeout_seconds` | 5 | 清理阶段上限，同时受总截止时间限制 |

日志中的 `waiting for bash（工具调用累计 202s，非静默计时）` 表示 DSH 当前工具调用已等待
多久，收到 worker 输出不会重置这个累计值。真正的静默时间见 watch 返回的 `silence_seconds`，
按最近一次计入活动重新计算；达到阈值才返回 `needs_attention / silent`。
`watch --wait-seconds 300` 则是单次观察窗口，到期返回 `review` 供 DSH 检查，worker 继续运行。
监督日志会分别标注“单次等待结束”或“连续无活动”，并记录当时的静默时间和阈值。

任何 worker 输出或可观察的界面变化都算活动，包括 spinner、计时器、重复错误和光标控制。
`pipe-pane` 统计原始输出字节，因此相同内容重画也能重置时钟；只在终端客户端本地绘制的
光标闪烁不可被 tmux 感知。默认 SDK 心跳也算活动，因此**持续心跳时 300 秒静默检测不会触发**。
DSH 仍须检查 watch 周期返回的屏幕，错误、选择和无效回执仍可提前触发判断，总超时仍有效。
如需只计 worker 活动，设置 `ConductorConfig(sdk_heartbeat_counts_as_activity=False)`，
CLI 使用 `--no-sdk-heartbeat-counts-as-activity`。关闭屏幕日志或回调不会关闭监督或 SDK 心跳记录。

网络或模型连接暂时故障时，DSH 根据现场发送“继续”或具体补交指令；交互选择由 DSH 根据任务
代做。首次普通菜单选择免费，重复卡住的菜单和主动恢复共用恢复预算，跨 watch 和业务返工不
清零，最多 5 次。明确无法恢复或额度耗尽时先保存末屏、停止 worker，再写 rejected；失败无需
有效回执。失败与总超时即使设置 `keep_session=True` 也会停止 worker，所有诊断文件仍保留。

Claude Code 和 Codex 每轮都必须将完整回答、关键结论、修改记录、检查结果及阻塞信息写入
`result.md`，长检查输出另存文件并在结果中引用。结果最终原子落盘后才能写入绑定 token 的
receipt；控制器和 SDK 都会拒绝缺失、空白或非 UTF-8 的结果文件。DSH 收到回执后读取完整结果
及相关引用文件，再独立检查工作区。`TaskResult.worker_result`（JSON 中同名字段）提供最后
一轮已有结果文件的路径；它是 worker 的报告，不替代 verdict。

conductor 会同时轮询两个候选 tmux 会话；DSH 选择哪个 agent 后，只有实际存在的会话产生屏幕日志。
会话名精确匹配，读屏和输入固定到启动时的 worker pane，切换 tmux 窗口不会改变控制目标。
启动时立即采样，之后默认每 10 秒采样当前屏幕及最近 5000 行历史，只有内容变化才发出日志事件，
不会随屏幕刷新触发采样。tmux 窗口创建时的历史上限为 50000 行。
SDK 可通过 `ConductorConfig(worker_log_interval_seconds=...)` 调整间隔，CLI 可通过
`--worker-log-interval-seconds` 调整。采样到的全部新增或替换行追加到 `worker-screen.log`；
实时 `RunEvent(source="claude"/"codex", kind="worker_output")` 回调每次最多展示 12 行。
Codex 的 `• Working (1m 05s • esc to interrupt)` 行会保留实际计时；下一次采样的计时变化
会作为替换行写入 `worker-screen.log` 和 worker 输出事件，用于观察 worker 活动。
大段重复历史会保存完整变化区域而不做昂贵的精细比较，因此诊断日志可能包含重复上下文。
运行结束时无需等待下个周期，在剩余清理预算内扩大到最多 50000 行历史补采一次。正常验收结束由 SDK 补采后
按 `keep_session` 清理会话；DSH 主动 stop 则先保存末屏到 observations，再关闭会话。
两个 skill 的 `capture` 同样默认读取 5000 行历史，可用 `--history-lines 50000` 扩大范围，
或用 `--history-lines 0` 只看当前屏幕。控制器处理菜单时始终只看当前屏幕，避免误认历史菜单。
备用屏幕、重绘或超出采样范围的输出仍可能遗漏，屏幕日志只用于观察状态；文件出现和屏幕文字
都不会被当作完成或验收信号，完整结果以 `result.md` 及引用文件为准。
屏幕日志与 watch 各自以 10 秒周期采样，DSH 协议事件仍实时回调。原始输出活动计数独立于日志
展示，不使用去重或 spinner 归一化结果；交接和菜单通常在下次 watch 检查时被发现。

状态目录默认位于当前 `workspace` 下的 `.dsh-conductor/`；可通过
`ConductorConfig(state_dir=...)` 或 CLI 的 `--state-dir` 指定其他位置：

```text
<workspace>/.dsh-conductor/runs/<run-id>/
├── request.json
├── runtime.json             # 资源归属、临时截止时间和最终清理报告
├── resources/               # 进程启动身份与已观察到的受管进程
├── run.lock                 # 防止恢复清理与活动运行竞争
├── user-prompt.md
├── manager-prompt.md
├── plan.json
├── verdict.json
├── worker-screen.log
├── supervision.json         # 活动时钟、恢复预算、当前轮次
├── supervision.jsonl        # 诊断、选择、恢复与停止审计
├── worker-activity.json     # 原始终端字节计数及最后输出时间
├── sdk-heartbeat.json       # SDK 心跳时间（计入活动时生成）
├── observations/            # DSH 操作前屏幕证据，stop 保存最终历史
├── sdk-stop.json            # SDK 已进入清理，阻止继续提交/恢复
├── sdk-stop-<agent>.txt      # SDK 兜底结束前的历史快照（发生时生成）
└── attempts/
    ├── claude/1/{task.md,result.md,receipt.json}
    └── codex/1/{task.md,result.md,receipt.json}
```

每轮运行使用新的 run id 和 receipt token，避免读取旧结论。accepted verdict 必须同时满足 DSH `turn/end` + `session.status=idle`、所有验收项通过、产物路径安全且选定 agent 的最终 receipt 有效。

## 开发验证

```bash
python3.13 -m compileall -q conductor skills tests examples
python3.13 -m unittest discover -v
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-claude-code
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-codex
```

## 发布

推送 GitHub Release 后，`.github/workflows/publish.yml` 会先在独立 job 中运行测试并构建
发行包，再用 PyPI Trusted Publishing 上传已验证的 wheel 与 sdist。首次发布前，需要在
PyPI 为 GitHub Actions 配置 Trusted Publisher，并在 GitHub 创建 `pypi` 环境。完整字段、
版本标签规则和发布步骤见 [`docs/pypi-publishing.md`](docs/pypi-publishing.md)。
