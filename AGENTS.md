# AGENTS.md — 项目说明

`dsh-conductor` 是一个 Python 3.13 SDK：DSH 读取调用方 prompt，拆解任务与验收标准，选择 Claude Code 或 Codex，在 tmux 中监督 worker，独立验证工作区并写出最终 verdict。

## 模块职责

| 模块 | 职责 |
|---|---|
| `conductor/sdk.py` | `Conductor`、配置、事件回调、运行生命周期和 `TaskResult` |
| `conductor/errors.py` | 错误码、阶段、原因链与兼容错误 JSON |
| `conductor/lifecycle.py` | 可取消等待、共享截止时间、工作区锁和有限子进程调用 |
| `conductor/runtime.py` | 私有 socket、资源元数据、清理报告与 `cleanup_run` |
| `conductor/processes.py` | Linux 进程启动身份、受管 session/组核验与终止 |
| `conductor/cli.py` | CLI 参数、stderr 事件渲染、JSON stdout、doctor/install/show |
| `conductor/dsh.py` | `dsh --profile sdk` JSON-RPC/stdio 客户端 |
| `conductor/state.py` | 运行目录、候选 agent 会话、attempt 和 receipt 路径 |
| `conductor/models.py` | plan、receipt、verdict 的 schema 与事实校验 |
| `conductor/prompt.py` | 发给 DSH 的中文拆解、委派、监督和验收契约 |
| `conductor/progress.py` | `RunEvent`、DSH 协议进度、心跳和 worker 日志事件 |
| `conductor/worker_log.py` | 轮询精确 tmux 会话并持久化屏幕变化 |
| `conductor/supervision.py` | 持久化活动时钟、恢复预算、DSH 选择/恢复/结束操作与证据 |
| `conductor/activity.py` | tmux 原始输出字节计数，保留重复输出与光标控制活动 |
| `conductor/skills.py` | skill 定位、复制到 workspace 与环境可用性检查 |
| `skills/tmux-claude-code` | Claude Code 专属 tmux 控制 skill |
| `skills/tmux-codex` | Codex 专属 tmux 控制 skill |
| `.github/workflows/publish.yml` | 分离构建与 OIDC 发布 job，上传 PyPI 发行包 |

## 不变量

1. Python 版本要求 3.13，运行时只使用标准库；项目不安装 DSH。
2. SDK 的业务输入只有 `workspace` 和完整 `prompt`；prompt 可指定 agent、任务和验收标准。
3. CLI `run` 只接受 `--workspace` 与 `--prompt`，不再接受 `--agent`、`--task`、`--verify`。
4. CLI stdout 只有一个 JSON 对象；实时进度由 `RunEvent` 回调交给调用方，CLI 再写 stderr。
5. DSH 是否完成只认 `turn/end.reason.kind` 与 `session.status == idle`。
6. worker 成功交接只认绑定 token 的 receipt 和非空 UTF-8 result.md；屏幕可用于诊断、选择、恢复和失败判断，不能作为成功依据；失败无需回执。
7. 验收只认 schema v2 verdict，并再次绑定 run id、plan agent、criterion id、receipt token 和工作区路径。
8. `DSH_PERMISSION_MODE` 必须是 `danger-full-access`，由 DSH 客户端强制设置。
9. 两个 skill 只有仓库中的源码；运行前直接覆盖到 `<workspace>/.dsh/skills`，不写入全局目录；修改 skill 时同步中文 `SKILL.md`。
10. tmux 文本通过 buffer/paste 传递，不能把自然语言直接拼入 shell 命令。
11. `project/` 是用户目录，除非用户明确要求，不修改、不删除、不提交。
12. 默认活动静默阈值 300 秒；任何 worker 输出/变化及 SDK 心跳计入活动。SDK 心跳可显式排除；持续心跳时静默检测不会触发，DSH 仍须检查周期返回的屏幕。
13. 主动恢复和重复卡住的菜单共用全 run 最多 5 次预算，不随 watch 或业务返工清零；首次普通选择免费。失败和总超时停止 worker，保留证据。
14. `timeout_seconds` 覆盖整个 run，清理预留 `min(cleanup_timeout_seconds, timeout_seconds * 0.1)`；阶段等待不得增加总预算。清理忽略调用方取消事件，保留首次执行错误。
15. 每个 SDK run 使用私有 tmux socket 和资源启动身份，不能对默认 server 或裸 PID 执行恢复清理。只有 accepted + keep_session 可保留 worker；清理状态必须可核验。
16. 同一 workspace 拒绝重叠 run。SDK 不改宿主信号处理器；CLI 在主线程临时处理 SIGINT/SIGTERM，清理后输出一个 JSON 并恢复处理器。

## 常用命令

```bash
python3.13 -m conductor install-skills --workspace /tmp/work
python3.13 -m conductor doctor
python3.13 -m conductor run --workspace /tmp/work --prompt '请使用 Codex 创建 a.txt。验收标准：a.txt 存在。'
python3.13 -m unittest discover -v
python3.13 -m compileall -q conductor skills tests
```

PyPI Trusted Publishing 的配置、版本标签和发布流程见 `docs/pypi-publishing.md`。

修改流程或话术时编辑 `conductor/prompt.py`；修改进度事件时编辑 `conductor/progress.py`；修改 agent 启动参数或弹窗规则时编辑对应 `conductor/agents/*.py` 和 skill 文档；修改日志来源时编辑 `conductor/worker_log.py`。
