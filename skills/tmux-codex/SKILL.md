---
name: tmux-codex
description: 在 tmux 中监督 Codex，提交任务、观察活动与回执、恢复连接故障、代做交互选择并保存失败证据。用于 DSH 委派给 Codex 的编码任务。
---

# 在 tmux 中监督 Codex

通过 Python 3.13 运行 `scripts/codex_session.py`，使用 `--dangerously-bypass-approvals-and-sandbox --no-alt-screen` 启动 worker。
只控制 request/plan 指定的本 agent 会话和工作区，所有返工沿用该会话。不得改用另一 agent。
会话名精确匹配，读屏与输入绑定启动时的 worker pane；切换观察窗口不会改变控制目标。
本轮使用 SDK 创建的私有 tmux socket，run/send/watch/recover/choose/stop 根据 --request
读取同目录 runtime.json；不得自行连接默认 socket 或启动另一服务器。status/capture/close
使用注入的 DSH_CONDUCTOR_SOCKET；手动执行时须传 --socket /tmp/dshc-.../tmux.sock。
调用方观察会话应使用 SDK 返回的 attach_command。

## 委派与文件交接

管理者提供 request、任务文件、receipt 路径和 token，优先原样使用管理者生成的精确命令。
任务正文留在 task 文件中，只将读取文件和交接协议的短指令通过 tmux buffer/paste 传入 TUI。
控制器使用 bracketed paste，先观察文字进入输入框，再单独发送 Enter。不要用逐字 send-keys
或把粘贴结束当作提交完成；Codex 可能把 Enter 吸收为草稿换行。

```bash
python3.13 <skill>/scripts/codex_session.py run \
  --request /absolute/run/request.json \
  --session dsh-codex-unique \
  --task-file /absolute/attempt/task.md \
  --receipt /absolute/attempt/receipt.json \
  --token UNIQUE_TOKEN
```

`run` 立即登记任务并返回 `starting`；随后必须 `watch`，由 watch 在输入界面就绪后提交任务。
不要把 run 的返回当成完成，不要因 watch 返回 running 而重新 run。只有收到有效回执且独立
验收后需要业务返工，才使用 `send`，参数与 run 相同，但取 request 中下一轮的文件和新 token。

每轮 worker 必须将完整回答、关键结论、修改记录、检查结果、阻塞信息写入 receipt 同目录的
非空 UTF-8 `result.md`。重要内容及时落盘，长检查输出另存文件并在报告中引用。最终报告先写
临时文件再原子替换，然后运行控制器提供的 `complete` 命令。`blocked` 也需保存报告。
终端只显示简短进度和路径。收到回执后，DSH 必须读完整报告及相关引用文件，再独立验收工作区。

## 有界观察与判断

```bash
python3.13 <skill>/scripts/codex_session.py watch \
  --request /absolute/run/request.json --session dsh-codex-unique --wait-seconds 60
```

watch 每 10 秒检查一次，单次最多等待 300 秒，且所有命令受本轮剩余执行时间限制。
工具执行超时应覆盖实际等待和命令开销；不得借新 watch、恢复或返工延长 SDK 截止时间。
等待和文件锁每约 100ms 检查截止时间/停止标记。sdk-stop.json 出现后不得继续派发、选择或恢复；
SDK 已接管清理。runtime.json 的 monotonic 截止时间只在本轮同一 boot 内有效，不用于跨重启恢复。
同步调用，不转入长期后台 `job_output` 等待。每次返回后先检查 status、当前屏幕、快照和实际文件：

| status | 管理者下一步 |
|---|---|
| `starting` | 尚在启动或等待输入界面就绪，继续 watch，不重复 run/send |
| `submitting` | 已开始粘贴，尚未确认 Codex 接收；继续 watch，不能宣称任务正在执行 |
| `running` | 已确认提交，检查屏幕和 observation；已停在输入框却无文件则诊断、补交 |
| `needs_attention` | 根据 reason、屏幕和 snapshot_file 决定观察、选择、恢复或结束 |
| `receipt_ready` | 有效 token 回执与结果文件已就绪，读取文件并独立验收；blocked 表示报告阻塞 |
| `stopped` | worker 已结束，记录失败证据并写 rejected verdict |

提交确认与 300 秒活动静默计时独立。`run/send/recover` 均先粘贴，watch 确认当前输入框有草稿后
发送一次 Enter，观察草稿清空才记录 `task_sent`。粘贴开始后 30 秒仍不能确认时返回
`needs_attention / submission_unconfirmed`，SDK 心跳、spinner 和光标变化不会延长该期限。
Enter 后草稿仍在时，下次采样提前返回 `submission_pending`，不要继续空等 300 秒。

遇到 `submission_pending`，查看当前屏幕，用 `choose --keys Enter` 补发一次提交；每次补发计入
全 run 最多 5 次恢复预算。控制器只允许单个 Enter，随后必须观察结果。展开的多行提示词和
`[Pasted Content N chars]` 都可能是草稿；历史用户消息也以 `›` 开头，应结合当前输入光标判断，
不能只搜索历史中的标记。草稿还在时禁止 recover 追加“继续”或重新粘贴任务。
遇到 `submission_unconfirmed`，任务是否启动尚不明确，先检查快照和当前屏幕；只有确认输入框
为空且未在忙时才 recover 重试，必要时有依据地 interrupt。无法确定或恢复耗尽则 stop，
不能用 acknowledge 或持续心跳把它解释为正常执行。以上状态均不代表任务成功，成功仍需文件回执。

默认连续 300 秒没有计入活动的变化才触发 `silent`。spinner、计时器、重复错误、终端输出字节、
光标位置或显示模式变化都算活动；无需判断内容价值。仅客户端本地绘制的光标闪烁不可从 tmux
读取，不凭空产生活动。SDK waiting 心跳默认也计入；request 中
`sdk_heartbeat_counts_as_activity=false` 时才排除。持续心跳会阻止静默超时，仍须检查每次
watch 到期返回的屏幕；不能因 SDK 还活着就认定 worker 正常。整个任务仍受 SDK 总时限约束。

Codex 执行中通常显示 `• Working (1m 05s • esc to interrupt)`。worker-screen.log 会保留其中
的实际计时，后续如变为 `1m 15s` 会记录替换行；检查 watch 返回的屏幕或日志时，把计时变化
视为 worker 活动。它不能单独证明网络或模型请求正常，也不是回执或成功证明。

菜单、错误提示、无效回执、worker 退出或监测管道失效会提前返回。错误匹配只是诊断线索，
不自动判失败。历史错误若已恢复，使用 `watch --acknowledge <id>` 继续观察；静默时间不清零。
若静默时有证据表明长命令正常执行，同样可 acknowledge，在本次 watch 时段内暂缓再次提醒。

## 主动恢复与选择

```bash
python3.13 <skill>/scripts/codex_session.py recover \
  --request /absolute/run/request.json --session dsh-codex-unique \
  --observation 3 --reason '连接已恢复，继续当前任务'

python3.13 <skill>/scripts/codex_session.py choose \
  --request /absolute/run/request.json --session dsh-codex-unique \
  --observation 4 --keys Down Enter --reason '选择符合任务要求的选项'
```

recover 默认发送“继续当前任务”及原任务交接协议，也可用 `--instruction-file` 提供补齐文件的
具体要求。仍在忙时不能盲目堆积输入；有证据需要中断才能恢复时显式加 `--interrupt`。
恢复沿用当前 attempt/token，不得跳到新一轮或伪造回执。无效回执先归档，由 worker 重写。
若有效回执已到达，控制器拒绝恢复，转入验收。

DSH 代为判断交互选择，明确当前选项及理由，再发送按键；不能一律选 Yes。屏幕变化导致
observation 过期时重新 watch。每次操作后都要观察是否生效。

主动恢复、补发提交 Enter 和反复卡住的菜单共享整个 run 的恢复预算，最多 5 次；首次普通菜单确认免费，
仅移动选项或勾选复选框不消耗恢复次数。
次数保存在 supervision.json，watch 重启及业务返工均不重置。预算用尽不得改文件绕过，必须结束。
网络/模型暂时故障可恢复；明确失败、无法修复的工具或凭据问题应结束，无需等待 receipt。

## 结束与排查

```bash
python3.13 <skill>/scripts/codex_session.py stop \
  --request /absolute/run/request.json --session dsh-codex-unique --reason '已无法恢复'
python3.13 <skill>/scripts/codex_session.py capture --session dsh-codex-unique --history-lines 50000
python3.13 <skill>/scripts/codex_session.py status --session dsh-codex-unique
```

stop 在剩余预算内先保存最多 50000 行历史，再停止会话，留下审计记录。失败时先 stop 再写 rejected verdict，
没有 receipt 不妨碍失败结论。正常验收后保留会话给 SDK 补采，再由 SDK 按 keep_session 配置清理；
只有 accepted 且 keep_session=true 才允许保留 worker。rejected、取消、失败和总超时必须停止
worker，即使 keep_session=true，诊断文件仍保留。SDK 会核验清理并返回 cleanup 报告；
cleanup_run/CLI cleanup 用于 SDK 结束后回收资源，不代表重新执行或重新验收任务。

capture 默认读取当前屏幕及最近 5000 行历史，`--history-lines 0` 只看当前屏幕；历史上限 50000。
Codex 的 --no-alt-screen 有助于保留历史，但重绘与历史容量仍可能造成遗漏。长文完整性由 result.md 及引用文件保障，屏幕仅用于状态和诊断。
控制器 stdout 为 JSON、stderr 为诊断。屏幕文字、文件出现或静止不能替代 token 回执，更不能
替代 DSH 独立 verdict。监督状态、恢复次数与操作依据见 run 目录的 supervision.json、
supervision.jsonl 和 observations/。
