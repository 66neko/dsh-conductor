# AGENTS.md — 项目说明

`dsh-conductor` 是一个 Python 3.13 SDK：DSH 读取调用方 prompt，拆解任务与验收标准，选择 Claude Code 或 Codex，在 tmux 中监督 worker，独立验证工作区并写出最终 verdict。

## 模块职责

| 模块 | 职责 |
|---|---|
| `conductor/sdk.py` | `Conductor`、配置、事件回调、运行生命周期和 `TaskResult` |
| `conductor/cli.py` | CLI 参数、stderr 事件渲染、JSON stdout、doctor/install/show |
| `conductor/dsh.py` | `dsh --profile sdk` JSON-RPC/stdio 客户端 |
| `conductor/state.py` | 运行目录、候选 agent 会话、attempt 和 receipt 路径 |
| `conductor/models.py` | plan、receipt、verdict 的 schema 与事实校验 |
| `conductor/prompt.py` | 发给 DSH 的中文拆解、委派、监督和验收契约 |
| `conductor/progress.py` | `RunEvent`、DSH 协议进度、心跳和 worker 日志事件 |
| `conductor/worker_log.py` | 轮询精确 tmux 会话并持久化屏幕变化 |
| `conductor/skills.py` | skill 定位、安装软链与环境可用性检查 |
| `skills/tmux-claude-code` | Claude Code 专属 tmux 控制 skill |
| `skills/tmux-codex` | Codex 专属 tmux 控制 skill |

## 不变量

1. Python 版本要求 3.13，运行时只使用标准库；项目不安装 DSH。
2. SDK 的业务输入只有 `workspace` 和完整 `prompt`；prompt 可指定 agent、任务和验收标准。
3. CLI `run` 只接受 `--workspace` 与 `--prompt`，不再接受 `--agent`、`--task`、`--verify`。
4. CLI stdout 只有一个 JSON 对象；实时进度由 `RunEvent` 回调交给调用方，CLI 再写 stderr。
5. DSH 是否完成只认 `turn/end.reason.kind` 与 `session.status == idle`。
6. worker 是否完成只认绑定 token 的 receipt 文件；不解析自然语言、屏幕稳定或产物出现。
7. 验收只认 schema v2 verdict，并再次绑定 run id、plan agent、criterion id、receipt token 和工作区路径。
8. `DSH_PERMISSION_MODE` 必须是 `danger-full-access`，由 DSH 客户端强制设置。
9. 两个 skill 只有仓库中的源码，安装操作只建立软链；修改 skill 时同步中文 `SKILL.md`。
10. tmux 文本通过 buffer/paste 传递，不能把自然语言直接拼入 shell 命令。
11. `project/` 是用户目录，除非用户明确要求，不修改、不删除、不提交。

## 常用命令

```bash
python3.13 -m conductor install-skills
python3.13 -m conductor doctor
python3.13 -m conductor run --workspace /tmp/work --prompt '请使用 Codex 创建 a.txt。验收标准：a.txt 存在。'
python3.13 -m unittest discover -v
python3.13 -m compileall -q conductor skills tests
```

修改流程或话术时编辑 `conductor/prompt.py`；修改进度事件时编辑 `conductor/progress.py`；修改 agent 启动参数或弹窗规则时编辑对应 `conductor/agents/*.py` 和 skill 文档；修改日志来源时编辑 `conductor/worker_log.py`。
