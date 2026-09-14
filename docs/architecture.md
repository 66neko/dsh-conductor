# 架构与协议

## 数据流

```text
Python 调用方
   │ workspace + prompt + on_event
   ▼
Conductor SDK ── JSON-RPC/stdio ──► DSH manager
   │                                  │
   │                                  ├─ 读取 user-prompt.md
   │                                  ├─ 生成 plan.json，选择一个 agent
   │                                  ├─ 通过对应 skill 启动 tmux worker
   │                                  ├─ 读取 receipt 并独立检查工作区
   │                                  └─ 生成 verdict.json
   ◄────────────── TaskResult + RunEvent ┘
```

`conductor` 不实现 LLM 调用、工具系统或会话持久化，这些职责属于工作环境中的 DSH。SDK 负责准备不可变运行目录、启动 DSH、转发协议事件、采集 tmux 屏幕变化，并校验 plan、receipt 和 verdict 的结构与身份。

## SDK 边界

`Conductor(workspace, config).run(prompt, on_event)` 是唯一高层入口。prompt 同时承载任务描述、验收标准和可选的 agent 偏好。DSH 必须服从 prompt 中明确的 Claude Code/Codex 选择；未指定时只能从当前可用 agent 中选择一个。SDK 不接受 `agent` 参数，也不从自然语言回复猜测选择结果。

`RunEvent` 是只读进度事件，包含相对耗时、来源、类型、可读消息和可选原始协议事件。DSH 事件和 worker 屏幕事件通过同一个回调串行交付。回调异常会被隔离，不能改变任务结果。

## 状态目录

每次运行创建新的 `runs/<run-id>/`：

```text
request.json          # 运行身份、两个候选 agent 和所有事实路径
user-prompt.md        # 调用方原始 prompt
manager-prompt.md     # 发给 DSH 的完整中文编排契约
plan.json             # DSH 选择的 agent、步骤和验收项
verdict.json          # DSH 独立验收结论
worker-screen.log     # tmux 屏幕变化
attempts/<agent>/<n>/
  task.md
  receipt.json
```

计划和 verdict 通过临时文件加 `os.replace` 原子写入。receipt token、路径和 attempt 都由 SDK 预先生成。状态目录位于工作区之外，避免 worker 的清理命令删除协议文件。

## 完成与验收事实

系统区分三类事实：

| 事实 | 证明什么 |
|---|---|
| token 匹配的 worker receipt | 本轮 worker 已交还控制权 |
| DSH `turn/end.reason.kind` 与 `session.status=idle` | 管理回合在协议层结束 |
| 通过 schema 校验的 `verdict.json` 且 status 为 accepted | DSH 已独立验证全部计划验收项 |

屏幕稳定、worker 自述、DSH 最后一条自然语言消息和产物偶然出现都不能替代这些事实。accepted verdict 还必须绑定选定 agent 的每一轮 receipt、覆盖全部 criterion id、只引用工作区内相对路径，并确保产物真实存在。

## 两个独立 skill

`skills/tmux-claude-code/` 和 `skills/tmux-codex/` 各自包含中文 `SKILL.md` 与控制器脚本。它们共享 tmux 文本传输和 token receipt 协议，但维护各自的启动参数、菜单提示和就绪规则。`conductor install-skills` 只创建指向仓库源目录的软链；SDK 不负责安装 DSH。

## tmux 日志采集

SDK 启动时为 Claude 和 Codex 两个候选会话各创建一个只读 `WorkerLogFollower`。采集器周期执行 `capture-pane`，归一化 spinner、计时器和交接命令噪声，只输出新增或替换的可见行。不存在的会话不会产生事件；DSH 选择并启动哪个 agent 后，那个会话的屏幕变化就会实时出现在 `on_event` 回调和 `worker-screen.log` 中。

Codex 使用 `--no-alt-screen`，通常可以看到最近滚动内容；Claude Code 的备用屏幕只保证当前可见内容。日志是可观测性通道，采集竞态或采集失败不会改变 DSH 结果。
