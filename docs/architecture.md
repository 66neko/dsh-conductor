# 架构与协议

## 数据流

```text
Python 调用方
   │ workspace + prompt + on_event + cancel_event
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

`Conductor(workspace, config).run(prompt, on_event, *, cancel_event=None)` 是唯一高层入口。prompt 同时承载任务描述、验收标准和可选的 agent 偏好。DSH 必须服从 prompt 中明确的 Claude Code/Codex 选择；未指定时只能从当前可用 agent 中选择一个。SDK 不接受 `agent` 参数，也不从自然语言回复猜测选择结果。

`RunEvent` 是只读进度事件，包含相对耗时、来源、类型、可读消息和可选原始协议事件。DSH 事件和 worker 屏幕事件通过同一个回调串行交付。回调异常会被隔离，不能改变任务结果。

## 状态目录

每次运行创建新的 `runs/<run-id>/`：

```text
runtime.json          # 资源身份、同 boot 的临时截止时间和清理报告
resources/            # DSH、tmux、worker、辅助命令的进程身份
run.lock              # 活动运行与恢复清理互斥
request.json          # 运行身份、两个候选 agent 和所有事实路径
user-prompt.md        # 调用方原始 prompt
manager-prompt.md     # 发给 DSH 的完整中文编排契约
plan.json             # DSH 选择的 agent、步骤和验收项
verdict.json          # DSH 独立验收结论
worker-screen.log     # tmux 屏幕变化
supervision.json      # 活动时间、恢复预算、轮次、最新 observation
supervision.jsonl     # DSH 观察、选择、恢复、停止的审计记录
worker-activity.json  # pipe-pane 原始输出字节计数和时间
sdk-heartbeat.json    # 默认计入活动的 SDK 心跳时间
observations/         # 需要判断时的屏幕快照与 stop 的末次历史
sdk-stop.json         # SDK 进入清理后的结束标记
sdk-stop-<agent>.txt  # SDK 结束前的历史快照
attempts/<agent>/<n>/
  task.md
  result.md            # worker 完整结果，必须先于 receipt 落盘
  receipt.json
```

计划和 verdict 通过临时文件加 `os.replace` 原子写入。receipt token、路径和 attempt 都由 SDK 预先生成。
每轮 `result_file` 固定为 receipt 同目录的 `result.md`，在 request 中预先公开给 DSH。
worker 必须先将完整结果原子落盘再写回执，包括 blocked 时的原因及已完成工作；`complete`、
receipt 加载和 accepted verdict 校验都会检查结果文件非空且为 UTF-8。DSH 收到合法回执后
完整读取结果及相关引用文件，并独立验证产物；结果文件出现本身不是完成信号。
状态目录默认位于工作区的 `.dsh-conductor/` 下；调用方可通过配置指定其他位置。

## 完成与验收事实

系统区分三类事实：

| 事实 | 证明什么 |
|---|---|
| token 匹配的 worker receipt | 本轮 worker 已交还控制权 |
| DSH `turn/end.reason.kind` 与 `session.status=idle` | 管理回合在协议层结束 |
| 通过 schema 校验的 `verdict.json` 且 status 为 accepted | DSH 已独立验证全部计划验收项 |

屏幕稳定、worker 自述、DSH 最后一条自然语言消息和产物偶然出现都不能替代这些事实。accepted verdict 还必须绑定选定 agent 的每一轮 receipt、覆盖全部 criterion id、只引用工作区内相对路径，并确保产物真实存在。

## 监督与恢复

`run/send` 只登记任务，`watch` 检查就绪后提交，之后每 10 秒检查一次回执、原始活动和屏幕。
单次 watch 最多等待 300 秒，返回当前 observation、屏幕和快照，供 DSH 判断；不把完整任务
阻塞在长期 job_output 内。任务成功交接仍只由回执证明，屏幕可作为选择、恢复和失败的证据。

活动时钟默认阈值 300 秒。`activity.py` 通过 tmux pipe-pane 记录字节量及真实输出时间；重复输出、
spinner、计时器、光标控制序列都会计入。当前屏幕和光标状态变化也计入，纯客户端闪烁无法观测。
SDK 心跳默认也计入（即使关闭回调或屏幕日志），持续心跳会阻止静默检测；可以配置
`sdk_heartbeat_counts_as_activity=False` 排除。活动不代表成功或健康，DSH 始终需要检查每次
watch 返回的屏幕；菜单、错误提示、无效回执、worker 退出或采集失效可提前触发 needs_attention。

recover 发送继续指令和原任务交接协议；choose 发送 DSH 根据当前菜单选择的按键。
所有动作核对本 run 的 agent/session/workspace 与最新 observation；恢复前再次检查 receipt，
已有合法交接则拒绝再发指令。无效回执归档后由 worker 重写，管理者不得代签。
监督操作通过文件锁互斥，watch 在睡眠时释放锁。恢复预算在副作用前持久化，整个 run 最多 5 次，
跨业务返工不清零；普通菜单首次选择免费，相同菜单反复出现消耗预算。业务返工轮数另由
max_attempts 控制，只有已交接任务才可进入下一轮。历史错误可 acknowledge 继续观察而不重置活动时间。

不可恢复失败或额度耗尽：stop 保存最终最多 50000 行并终止 worker，再由 DSH 写 rejected，
无需 receipt。SDK 在 rejected、异常、总超时后也会检查候选会话身份、保存历史并停止 worker，
即使 keep_session=true；sdk-stop.json 阻止遗留 watch/recover 继续操作。SDK 不伪造验收结论，
协议未完成或总超时仍返回 ConductorError。timeout_seconds 默认 3600，覆盖整个 run（包括初始化与清理），不因活动或恢复重置。

## 两个独立 skill

`skills/tmux-claude-code/` 和 `skills/tmux-codex/` 各自包含中文 `SKILL.md` 与控制器脚本。它们共享 tmux 文本传输和 token receipt 协议，但维护各自的启动参数、菜单提示和就绪规则。SDK 在启动 DSH 前，将两个 skill 直接复制到 `<workspace>/.dsh/skills/` 并覆盖同名目录；该目录是 DSH 的项目级 skill 根，不会污染 `~/.dsh/skills`。`conductor install-skills --workspace <path>` 可以手动提前完成复制；SDK 不负责安装 DSH。

## tmux 日志采集

SDK 启动时为 Claude 和 Codex 两个候选会话各创建一个只读 `WorkerLogFollower`。采集器立即
采样一次，然后默认每 10 秒
执行 `capture-pane -S -5000`，读取当前屏幕和最近 5000 行历史，归一化 spinner、计时器和
交接命令噪声。全部新增或替换行写入 `worker-screen.log`；只有 `on_event` 实时展示做去重并
限制为最后 12 行。大段重复历史采用有界比较并保存完整变化区域，可能带有重复上下文，避免结束补采时耗费过多时间。不存在的会话不会产生事件。两个 skill 的 `capture` 默认也读取 5000 行历史，
可通过 `--history-lines` 调整；菜单和就绪识别仍只读取当前屏幕。

周期采集与屏幕刷新无关，仅在内容变化时输出日志事件。停止时在清理剩余预算内补采最多 50000 行历史，
不等待下个周期。DSH 写入 verdict 后保留会话；SDK 完成补采并验证结果后，才在
`keep_session=False` 时关闭所选会话，关闭前核对 agent 与工作区元数据。失败由 stop 或 SDK 先保存
历史快照再关闭会话，保留文件证据；不再让异常 worker 继续运行。
DSH 协议事件与控制器菜单、receipt 检查不受屏幕日志采样间隔影响。

tmux 先创建占位窗口，在本会话设置 `history-limit=50000` 后再创建 worker 窗口，最后关闭
占位窗口并启动 agent。这使历史上限实际作用于 worker，且不修改用户 tmux 的全局选项。
控制器使用精确会话名并持久化 worker pane ID；切换窗口不会重定向读屏或输入，同前缀会话也不会被误操作。

Codex 使用 `--no-alt-screen`，通常可以看到最近滚动内容；Claude Code 使用备用屏幕或原地重绘
时，历史仍可能不可恢复。扩大范围不能保证完整记录终端输出；完整内容通过 `result.md` 及其
引用文件交付，tmux 只提供运行状态和诊断信息。采集竞态或采集失败不会改变 DSH 结果。


## 生命周期与资源所有权

`lifecycle.py` 提供每轮 RunContext、共享截止时间和可取消等待；`errors.py` 定义稳定的
错误码协议。预算从 run 入口开始，清理阶段不再检查调用方取消事件。首次终止原因保留。
DSH 使用无缓冲、非阻塞管道与 selectors；协议读写、初始化和关闭不会等待无限的管道 EOF。
只有合法 turn/end 和 idle 同时成立才完成，非法 JSON 帧立即成为协议错误。

每个 run 使用 `/tmp/dshc-*/tmux.sock` 私有服务器。DSH 与 worker 分别具有登记的进程身份；
进程启动时间、UID、boot ID 和 session/进程组用于防止 PID 复用时误杀。清理先停止管理者，
再回收 worker 与辅助资源，并核验资源是否存活。不能确认归属或退出时报告 incomplete。
同一工作区使用独立于 state_dir 的 workspace.lock 拒绝重入，避免复制 skill 与任务编辑竞争。

runtime.json 与 resources 是可变运行元数据，request/plan/receipt/verdict 保持审计用途。
cleanup_run 取得 run.lock 后可以在 SDK 崩溃后回收已登记资源；活动运行不能被该接口接管。
持久化 monotonic 截止时间只供同一次启动、同一 boot 的控制器使用，恢复清理不复用它。
保留 worker 只适用于 accepted + keep_session；后续观察使用包含 socket 的 attach_command。
详细契约和无法回收的边界见 [运行生命周期](lifecycle.md)。
