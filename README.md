# dsh-conductor

`dsh-conductor` 让 DSH（DeepSeek Harness）管理 Claude Code 或 Codex：下级 agent 在独立
tmux 会话中实现任务，DSH 在工作区中独立验收，调用方只接收经过结构校验的 JSON verdict。

## 核心约束

- DSH 是唯一管理者和验收者。conductor 不用规则引擎代替模型判断工作质量。
- Claude Code 与 Codex 由两个独立 skill 驱动：`tmux-claude-code` 和 `tmux-codex`。
- 每轮 worker 完成由唯一 token 的 receipt 文件确认，不解析自然语言，也不等待屏幕“稳定”。
- receipt 只表示 worker 已停止编辑、可以验收。DSH 必须自己读文件并运行验证命令。
- verdict 只对本次唯一 `run_id` 有效；conductor 会验证 run id、agent、轮次、check 和产物路径。
- `conductor run` 的 stdout 永远只有一份 JSON。所有进度和诊断写入 stderr。
- 所有 Python 代码要求 Python 3.13，不依赖第三方 Python 包。

## 安装与检查

需要 Python 3.13、tmux、DSH，以及 Claude Code 或 Codex 中至少一个。当前 DSH 要求 Node
`^22.19.0 || >=24.0.0`；conductor 会检查 `import.meta.main` 能力，并从 PATH 或 nvm 安装目录
选择可用版本。可通过 `DSH_NODE=/absolute/path/to/node` 显式指定。

```bash
python3.13 -m conductor install-skills
python3.13 -m conductor doctor
```

`install-skills` 将仓库中的两个 skill 软链到 `$DSH_HOME/skills/`。仓库目录始终是 skill
唯一源；若目标位置已有真实目录，命令会先备份它。

可选的命令安装：

```bash
python3.13 -m pip install -e .
```

## 运行

调用方必须明确选择 agent，并提供可验证的验收标准：

```bash
python3.13 -m conductor run \
  --workspace /path/to/project \
  --agent claude \
  --task "创建 hello.txt，内容为 Hello 后跟一个换行" \
  --verify "hello.txt 存在；字节内容恰好是 b'Hello\\n'"
```

使用 Codex 时只需改为 `--agent codex`。没有默认 agent，也没有默认验收标准。

成功时 stdout：

```json
{
  "schema_version": 1,
  "run_id": "20260914T120000.000000Z-a1b2c3d4e5f6",
  "status": "accepted",
  "agent": "claude",
  "attempts": 1,
  "artifacts": ["hello.txt"],
  "checks": [
    {
      "criterion": "hello.txt 的精确字节内容",
      "method": "python3.13 读取 bytes 并比较",
      "evidence": "读取结果为 b'Hello\\n'",
      "passed": true
    }
  ],
  "summary": "全部验收标准通过",
  "remaining_issues": [],
  "workspace": "/path/to/project",
  "state_directory": "/home/user/.local/state/dsh-conductor/runs/...",
  "session": "dsh-claude-a1b2c3d4e5f6",
  "dsh": {
    "status": "completed",
    "elapsed_seconds": 42.1,
    "event_count": 18
  }
}
```

退出码：`0` 表示 DSH 正常结束且 verdict 为 `accepted`；`1` 表示 rejected、DSH 错误、
超时或 verdict 非法；`2` 表示调用参数或运行前提错误。

常用选项：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--max-attempts` | `2` | DSH 最多提交多少轮 worker 任务 |
| `--attempt-timeout-seconds` | `1200` | 每轮等待 token receipt 的上限 |
| `--timeout-seconds` | `3600` | DSH 整体运行上限 |
| `--provider` | `deepseek-official` | DSH provider |
| `--model` | `deepseek-flash` | DSH 管理模型 |
| `--keep-session` | 关闭 | verdict 落盘后保留 worker tmux 会话 |
| `--quiet` | 关闭 | 不输出 stderr 进度 |
| `--state-dir` | XDG state 目录 | 覆盖审计状态位置 |

查看最近一次运行清单和 verdict：

```bash
python3.13 -m conductor show
python3.13 -m conductor show --run-id <run-id>
```

## 运行状态

状态默认位于 `$XDG_STATE_HOME/dsh-conductor`，未设置时位于
`~/.local/state/dsh-conductor`：

```text
runs/<run-id>/
├── request.json
├── acceptance.md
├── orchestrator-prompt.md
├── verdict.json
└── attempts/
    ├── 1/
    │   ├── task.md
    │   └── receipt.json
    └── 2/
        ├── task.md
        └── receipt.json
```

状态不放在工作区内，因此 worker 执行清理命令或用户项目使用 `git clean` 时不会误删协议文件。
每次运行使用新目录，不需要先删除旧 verdict，也不存在旧文件被当作本轮结果的问题。

## Skill 接口

两个 skill 的命令结构相同，但没有通用的 `--agent` 开关。每个 skill 只控制自己的 TUI：

```bash
python3.13 ~/.dsh/skills/tmux-claude-code/scripts/claude_session.py --help
python3.13 ~/.dsh/skills/tmux-codex/scripts/codex_session.py --help
```

主要子命令为 `run`、`send`、`status`、`capture`、`close` 和内部交接命令 `complete`。
会话带有 agent 与 workspace 元数据；Claude 控制器不能误接管 Codex 会话，反之亦然。

## 开发验证

```bash
python3.13 -m compileall -q conductor skills tests
python3.13 -m unittest discover -v
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-claude-code
python3.13 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tmux-codex
```

详细协议与失败语义见 [架构文档](docs/architecture.md)。
