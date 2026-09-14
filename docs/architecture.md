# 架构与协议

## 数据流

```text
调用方
  │ conductor run
  ▼
conductor ── JSON-RPC/stdio ──► DSH manager
  │                              │ load selected skill
  │                              ▼
  │                         agent-specific controller
  │                              │ tmux buffer/paste
  │                              ▼
  │                         Claude Code or Codex
  │                              │ token receipt
  │                              ▼
  │                         DSH independent checks
  │                              │ atomic verdict
  ◄──────────────────────────────┘
```

conductor 不解释 DSH 或 worker 的自然语言。它只管理进程、协议事实和持久化记录的结构。

## 三种事实

系统区分三种含义，不能互相替代：

| 事实 | 证明什么 | 不证明什么 |
|---|---|---|
| worker receipt 存在且 token 匹配 | 本轮 worker 已交还控制权 | 产物正确 |
| DSH `turn/end` 为 completed 且 session idle | 管理回合在协议层正常收敛 | verdict 合法或验收通过 |
| 当前 run 的 verdict 合法且 accepted | DSH 声明独立检查通过，且 conductor 的结构约束成立 | 验收标准本身足够严格 |

receipt 的 token、文件路径和 attempt 都由 conductor 在委派前生成。每轮使用不同路径，controller
拒绝覆盖已有 receipt。verdict 所在 run 目录也是唯一的，因此不会读取到旧运行的结论。

## Worker 交接

controller 将用户任务和一段交接协议一起粘贴到 TUI。协议要求 worker 的最后一个工具动作为：

```text
<agent-specific script> complete --receipt <path> --token <token> ...
```

`complete` 以临时文件加 `os.replace` 原子写 receipt。controller 轮询文件、解析 JSON、校验 token
和状态。TUI 退出、receipt 非法或超时都返回失败，并保留 tmux 会话供诊断。

这条协议没有把验收交给 worker。worker 仍可能误判自己的工作，DSH 后续必须从工作区重新取证。

## 两个独立适配器

`conductor/tmux.py` 只实现 tmux 机制。以下差异由各自适配器维护：

| 行为 | Claude Code | Codex |
|---|---|---|
| 启动参数 | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox --no-alt-screen` |
| 菜单光标 | `❯` | `›` |
| 菜单提示 | `Enter to confirm` 等 | `Press enter to continue` 等 |
| 就绪信号 | 输入光标或 bypass 状态 | 输入光标或 YOLO 状态 |

首次目录信任菜单可能在初始主界面出现之后才弹出。controller 因此在启动阶段和等待 receipt
阶段都处理已知菜单；若提交任务的回车被菜单消费，菜单关闭后只重新提交一次。

Codex 对长文本使用 bracketed paste，界面可能先显示 `[Pasted Content N chars]`，再异步完成
编辑器更新。若最初的 Enter 早于更新完成，controller 只在该明确的 pending-paste 标记仍存在时
节流重发 Enter；它不会根据屏幕静止或自然语言猜测任务是否已提交。

## 字面文本传输

自然语言不进入 shell 命令，也不使用 `tmux send-keys -l` 承载长文本。controller 将完整 prompt
写入命名 tmux buffer，再通过 `paste-buffer` 送入 TUI，最后单独发送 Enter。这保留换行、引号、
`$()` 和形似按键名的文本，也绕开命令行长度限制。

## DSH 完成条件

DSH SDK profile 的 stdout 是 newline-delimited JSON-RPC 2.0。client 同时观察：

```text
session.event(type=turn/end, reason.kind=...)
session.status(status=idle)
```

两条通知可能交换顺序，因此 client 保存两个状态并在每次更新后重新判断。进程提前退出、RPC
超时或没有 turn end 都映射为错误，不能因 stdout 暂停或 DSH 最后一条消息看似完成而成功。

## Verdict 防线

DSH 写出的 verdict 还要经过 conductor 校验：

- `schema_version`、`run_id` 和 `agent` 与当前请求一致；
- `attempts` 在允许范围内；
- accepted 引用的每轮 receipt 都存在、token 匹配，且最后一轮状态为
  `ready_for_verification`；
- accepted 至少包含一个 check，且所有 check 的 `passed` 都为 true；
- artifact 必须是工作区内的相对路径，accepted 时文件必须真实存在；
- accepted 不能带 remaining issues。

这些是结构和身份约束。任务语义与验收充分性仍由 DSH 负责，因此调用方应提供具体、可执行的
`--verify`。

## 状态与恢复

运行目录位于 XDG state，而非工作区。`request.json` 与 `orchestrator-prompt.md` 保留精确输入，
每轮 task/receipt 记录返工过程，`verdict.json` 保存最终结论。失败后可以运行
`conductor show --run-id ...`，并根据输出中的 session 名 attach 到仍存活的 worker。
