# 取消、时间预算与资源清理

本文描述 0.5.0 的生命周期契约。Python 3.13、标准库运行时、业务 prompt 和
schema v2 verdict 保持不变。完整资源身份核验使用 Linux `/proc`，支持 Linux/WSL2。

## 同步调用与取消

```python
def run(self, prompt, on_event=None, *, cancel_event=None) -> TaskResult: ...
def cleanup_run(state_directory, *, timeout_seconds=5.0) -> CleanupReport: ...
```

`run(prompt)` 和 `run(prompt, callback)` 保持可用。cancel_event 是调用方的
`threading.Event`，SDK 不 clear、不跨轮复用。调用方可以在另一个线程设置事件。
未传入时，本轮创建内部事件。取消不会向 worker 提交自然语言“停止”指令。

| 时机 | 行为 |
|---|---|
| 调用前已设置事件 | cancelled，phase=validation，不准备 skill 或创建运行目录 |
| 准备、初始化、管道读写、运行和校验 | 在可中断检查点终止执行，进入清理 |
| 已进入清理 | 执行结果已经确定，继续清理；新取消不覆盖原终止原因 |
| 已返回 | 事件不再影响此前结果 |

库控制的等待以约 100ms 的检查间隔响应；实际返回还包含清理。SDK 不安装信号处理器。
KeyboardInterrupt/SystemExit 在清理后保持传播；CLI 有独立的信号路径。

同一 workspace 的重叠 run 被 workspace.lock 拒绝，返回 workspace_busy。该锁不随
state_dir 改变。不同工作区独立运行。SDK 不保证同一工作区内多个 worker 并发编辑安全。

## 时间预算

timeout_seconds 默认 3600 秒，从进入 run 开始覆盖准备、DSH 初始化、执行、结果校验及清理。
cleanup_timeout_seconds 默认 5 秒。所有计时使用单调时钟：

```text
cleanup_reserve = min(cleanup_timeout_seconds, timeout_seconds * 0.1)
total_deadline = started + timeout_seconds
execution_deadline = total_deadline - cleanup_reserve
cleanup_deadline = min(total_deadline, cleanup_started + cleanup_timeout_seconds)
```

600 秒总预算、5 秒清理上限，执行最晚在 595 秒结束；2 秒总预算仅预留 0.2 秒清理。
很短的预算可能无法完成全部核验，此时如实报告 incomplete，调用方可重试 cleanup_run。
阶段 timeout 只能缩短等待。dsh_init_timeout_seconds 默认 30 秒；原
dsh_shutdown_timeout_seconds 默认 5 秒，仍只是 DSH 关闭阶段的上限，不能增加清理总预算。

worker_idle_timeout_seconds 仍是活动静默阈值，不是 attempt 的执行时限；SDK 心跳仍默认
计入活动。watch、业务返工和恢复共享原截止时间，最多 5 次恢复的规则不变。
控制器从 runtime.json 读取截止时间时验证 run 身份、active 状态和 boot ID。
cleanup_run 不读取旧执行截止时间，而使用本次调用自己的清理预算。

时间预算约束 SDK 的调度与可控制等待，不是硬实时保证。同步文件系统调用、进程创建、
内核不可中断 I/O 和调度延迟可能超过截止点；任意 Python 回调也无法被安全强制终止。
需要硬上限的外部 Runtime 应另设进程/容器终止期限。

## 清理与保留

每轮使用短路径 `/tmp/dshc-*/tmux.sock`，目录权限 0700。所有受管 tmux 操作显式使用该
socket，不修改宿主全局环境或用户 tmux 配置。DSH 使用独立进程 session/组；控制器、
worker、tmux server 与辅助命令登记在 resources/，包含启动身份而非仅有 PID。

清理先写 sdk-stop.json 并停止 DSH/工具，随后在剩余预算内补采日志、停止 worker、核验
登记资源，最后停止事件派发。异常时使用 TERM/KILL。清理不依赖 DSH 继续执行关闭指令。

| 执行结果 | keep_session=False | keep_session=True |
|---|---|---|
| accepted | 清理 worker | 保留已验收 worker，状态 retained |
| rejected | 清理 | 清理 |
| 取消、超时、协议/结果错误 | 清理 | 清理 |

保留仅限已验收的所选会话及其 worker；其他候选或已替换 worker 的残留进程仍会回收。
保留会话时停止 SDK 的活动采集；观察用 result.attach_command，完成观察后调用
cleanup_run(result.state_directory)。只有实际保留了会话才返回 retained。
run 返回后不能仅凭 session 字段判断会话仍存在。

CleanupReport 的 status：

- completed：本轮已登记、可核验的必需资源已退出；空资源也属于 completed。
- retained：必需收尾已完成，并按配置保留 worker，列入 retained_resources。
- incomplete：存在存活资源或无法确认的资源，列入 remaining_resources。

errors 记录诊断，包括补采失败、展示事件丢弃和未返回的调用方回调；诊断存在本身不代表
必需资源未回收。timed_out 表示清理发生截止时间耗尽，独立于主错误的 timed_out。
caller callback 是调用方代码；SDK 停止后续派发，但不会把它伪装成已被终止。

原执行错误始终优先。若无执行错误但必需清理 incomplete，抛 cleanup_failed；
exc.result 保存已校验 TaskResult，并带同一 cleanup 报告，原文件也保留。
这条规则也适用于已生成合法 rejected 的运行。它不把业务拒绝改成异常，异常原因是清理。

## 崩溃后恢复清理

```bash
conductor cleanup --state-directory /absolute/workspace/.dsh-conductor/runs/run-id
```

cleanup_run 根据 runtime.json、request.json、resources/ 验证归属，取得 run.lock 后执行。
活动 run 或另一个清理调用持锁时返回 incomplete，不接管执行。重复调用是幂等的；资源
已消失视为完成。旧记录缺少元数据时返回诊断，不猜测 PID、扫描默认 tmux 或按程序名杀进程。
清理保存 request、plan、receipt、verdict、产物和日志，更新 runtime.json 的清理报告。

SIGKILL、宿主机故障以及自行 setsid 脱离登记范围的任意后台程序不能仅靠 Python finally
保证回收。资源创建与落盘之间也存在崩溃窗口。报告只描述已登记和可核验资源；强隔离与
完整进程树收容应由外部 Runtime 的容器/cgroup 提供。

## 回调与 CLI

Conductor 的 on_event 在独立派发线程按入队顺序串行调用；不要依赖调用线程局部变量。
回调异常不影响业务结论。默认队列最多 1024 条；积压时丢弃旧展示事件，保留最新进度。
持久化审计与 worker 报告不受此丢弃策略影响。退出时只在剩余预算内尝试派发尾部事件，
随后停止派发。已经进入的用户回调可能晚于 run 返回；结果判断使用返回值/异常。

CLI 仅在主线程临时处理 SIGINT/SIGTERM，handler 只记信号和设置 Event；结束后恢复
原处理器。run 的 stdout 是一个最终 JSON，进度写 stderr。help/version 保持普通 CLI 文本。

| 结果 | 退出码 |
|---|---:|
| accepted | 0 |
| rejected、输入/配置/普通执行错误 | 1 |
| timeout | 124 |
| SIGINT 取消 | 130 |
| SIGTERM 取消 | 143 |

## 从 0.4.1 迁移

- 原调用签名、ConductorError 捕获、str(exc)、run_id/state_directory 和旧 JSON 字段保留。
- 新增 cleanup、错误分类、tmux_socket 和 attach_command；顶层 schema_version 仍是 1，
  plan=1、verdict=2、receipt=1 不变。JSON 消费者应容忍新增字段。
- timeout_seconds 从 DSH 回合时限变为整个 run 总预算；需要时上调调用方预算。
- 取消/超时后的 CLI 退出码更具体；原来只检查非零的脚本无需改变。
- 清理失败可能使已经验收的任务抛 cleanup_failed，应检查 exc.result，避免无条件重试。
- 保留会话改用私有 socket；不要再拼接不含 -S 的 attach/kill 命令。
- ConductorConfig 的非法值仍是 ValueError；CLI 把配置错误序列化成 invalid_config。
