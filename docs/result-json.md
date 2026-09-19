# 任务返回 JSON 格式

本文描述 `Conductor.run()` 完成后产生的最终结果，以及 CLI `conductor run` 写入
stdout 的 JSON 契约。实时进度事件使用另一套格式，见[实时事件](#实时事件-runevent)。

## 获取 JSON

Python SDK 返回的是 `TaskResult` 对象。调用 `to_json()` 后得到可直接交给
`json.dump()` 或 `json.dumps()` 的字典：

```python
import json

from conductor import Conductor

result = Conductor("/path/to/project").run(prompt)
payload = result.to_json()
print(json.dumps(payload, ensure_ascii=False, indent=2))
```

CLI 会把同一个 JSON 对象写入 stdout：

```bash
python3.13 -m conductor run \
  --workspace /path/to/project \
  --prompt '请使用 Codex 创建 hello.txt。验收标准：文件存在。' \
  > result.json
```

实时日志由 CLI 写入 stderr，不会破坏 `result.json`。CLI 对 `accepted` 返回退出码
`0`，对 `rejected` 和 `error` 返回退出码 `1`。

## 完整示例

下面是一次通过验收的完整示例。示例中的 run id、路径、耗时和文本均会随运行变化：

```json
{
  "schema_version": 1,
  "run_id": "20260914T071530.123456Z-a1b2c3d4e5f6",
  "status": "accepted",
  "workspace": "/home/user/projects/demo",
  "state_directory": "/home/user/projects/demo/.dsh-conductor/runs/20260914T071530.123456Z-a1b2c3d4e5f6",
  "session": "dsh-codex-a1b2c3d4e5f6",
  "plan": {
    "schema_version": 1,
    "run_id": "20260914T071530.123456Z-a1b2c3d4e5f6",
    "agent": "codex",
    "agent_reason": "用户在 prompt 中明确要求使用 Codex",
    "task_summary": "创建内容符合要求的 hello.txt",
    "implementation_steps": [
      "在工作区根目录创建 hello.txt",
      "检查文件内容和末尾换行"
    ],
    "acceptance_criteria": [
      {
        "id": "file-exists",
        "description": "hello.txt 存在"
      },
      {
        "id": "exact-content",
        "description": "文件字节内容恰好为 Hello Conductor 加一个换行"
      }
    ]
  },
  "verdict": {
    "schema_version": 2,
    "run_id": "20260914T071530.123456Z-a1b2c3d4e5f6",
    "status": "accepted",
    "agent": "codex",
    "attempts": 1,
    "artifacts": [
      "hello.txt"
    ],
    "checks": [
      {
        "criterion_id": "file-exists",
        "criterion": "hello.txt 存在",
        "method": "使用 test -f hello.txt 检查文件",
        "evidence": "命令退出码为 0",
        "passed": true
      },
      {
        "criterion_id": "exact-content",
        "criterion": "文件字节内容恰好为 Hello Conductor 加一个换行",
        "method": "使用 Python 读取文件并比较 bytes",
        "evidence": "读取结果为 b'Hello Conductor\\n'",
        "passed": true
      }
    ],
    "summary": "全部验收标准均已通过",
    "remaining_issues": []
  },
  "dsh": {
    "status": "completed",
    "elapsed_seconds": 42.731,
    "event_count": 19,
    "final_text": "任务已执行并完成独立验收。"
  },
  "worker_log": "/home/user/projects/demo/.dsh-conductor/runs/20260914T071530.123456Z-a1b2c3d4e5f6/worker-screen.log"
}
```

## 顶层字段

| 字段 | JSON 类型 | 必需 | 含义 |
|---|---|---:|---|
| `schema_version` | integer | 是 | 顶层结果 schema 版本，当前固定为 `1`。它与嵌套对象的版本分别演进。 |
| `run_id` | string | 是 | 本次运行的唯一标识，格式为 UTC 时间戳加随机后缀。 |
| `status` | string | 是 | DSH 验收结论，只能是 `accepted` 或 `rejected`。 |
| `workspace` | string | 是 | worker 修改并由 DSH 验收的工作目录，使用解析后的绝对路径。 |
| `state_directory` | string | 是 | 本轮运行记录目录的绝对路径。计划、验收结论、prompt 和回执均保存在这里。 |
| `session` | string | 是 | 被选中 worker 的 tmux 会话名，例如 `dsh-codex-a1b2c3d4e5f6`。会话可能已按运行配置关闭。 |
| `plan` | object | 是 | DSH 在执行前产生的结构化任务计划，格式见 [`plan`](#plan-对象)。 |
| `verdict` | object | 是 | DSH 独立验收后产生的结论，格式见 [`verdict`](#verdict-对象)。 |
| `dsh` | object | 是 | 本次 DSH 管理回合的协议摘要，格式见 [`dsh`](#dsh-对象)。 |
| `worker_log` | string | 否 | `worker-screen.log` 的绝对路径。关闭采集或没有产生日志文件时字段会被省略，不会返回 `null`。 |
| `worker_result` | string | 否 | 所选 agent 最后一轮 `result.md` 的绝对路径。未提交 worker 或本轮未生成文件时省略；报告须结合 verdict 判断，文件存在不代表通过验收。 |

`TaskResult.accepted` 是 Python 对象上的便捷布尔属性，等价于
`result.verdict.status == "accepted"`。它不会作为单独字段写入 JSON；JSON 调用方应检查
顶层 `status`。

## `plan` 对象

`plan` 记录 DSH 对原始 prompt 的拆解结果和 agent 选择。它在 worker 开始任务前生成。

| 字段 | JSON 类型 | 约束与含义 |
|---|---|---|
| `schema_version` | integer | 当前固定为 `1`。 |
| `run_id` | string | 必须与顶层 `run_id` 完全一致。 |
| `agent` | string | 只能是 `claude` 或 `codex`。选择必须遵从 prompt 中明确指定的 agent；未指定时由 DSH 在可用 agent 中选择。 |
| `agent_reason` | string | DSH 选择该 agent 的非空理由。 |
| `task_summary` | string | DSH 对本次任务的非空摘要。 |
| `implementation_steps` | array[string] | 非空执行步骤数组，每一项都是非空字符串。 |
| `acceptance_criteria` | array[object] | 非空验收标准数组。最终 `verdict.checks` 必须逐项覆盖这里的所有 ID。 |

每个 `acceptance_criteria` 元素包含：

| 字段 | JSON 类型 | 约束与含义 |
|---|---|---|
| `id` | string | 本轮计划内唯一的验收项 ID。长度为 1 到 64 个字符，首字符必须是字母或数字，其余字符可使用字母、数字、下划线和连字符。 |
| `description` | string | 可独立验证的非空验收要求。 |

## `verdict` 对象

`verdict` 是 DSH 在 worker 交回控制权后，直接检查工作区得到的独立验收记录。

| 字段 | JSON 类型 | 约束与含义 |
|---|---|---|
| `schema_version` | integer | 当前固定为 `2`。 |
| `run_id` | string | 必须与顶层和 `plan.run_id` 完全一致。 |
| `status` | string | `accepted` 或 `rejected`，并与顶层 `status` 相同。 |
| `agent` | string | `claude` 或 `codex`，必须与 `plan.agent` 相同。 |
| `attempts` | integer | 实际委派次数，范围为 `0` 到 `ConductorConfig.max_attempts`。`accepted` 至少需要一次委派。 |
| `artifacts` | array[string] | 本次任务的产物路径。每项必须是工作区内的相对路径；`accepted` 时列出的产物必须真实存在。 |
| `checks` | array[object] | 逐项验收记录。必须恰好覆盖 `plan.acceptance_criteria` 的全部 ID，每个 ID 只出现一次。 |
| `summary` | string | DSH 对最终验收结论的非空总结。 |
| `remaining_issues` | array[string] | 未解决问题。`accepted` 时必须为空；`rejected` 时必须至少包含一项非空说明。 |

每个 `checks` 元素包含：

| 字段 | JSON 类型 | 约束与含义 |
|---|---|---|
| `criterion_id` | string | 对应 `plan.acceptance_criteria[].id`。 |
| `criterion` | string | 本次检查所对应的验收要求。 |
| `method` | string | DSH 实际使用的检查方法或命令说明。 |
| `evidence` | string | 检查所得的非空事实证据。 |
| `passed` | boolean | 该项是否通过。`accepted` 要求所有检查均为 `true`。 |

除上述结构校验外，`accepted` 还要求选中 agent 的最后一次 token 回执状态为
`ready_for_verification`。回执属于运行目录中的内部审计记录，不嵌入最终 JSON。

## `dsh` 对象

| 字段 | JSON 类型 | 含义 |
|---|---|---|
| `status` | string | DSH 管理回合的协议状态。能够生成 `TaskResult` 时为 `completed`。 |
| `elapsed_seconds` | number | DSH 回合耗时，单位为秒，JSON 中保留三位小数。 |
| `event_count` | integer | SDK 在本轮收到的 DSH 协议事件数量。 |
| `final_text` | string | DSH 最后一段文本，仅供展示和审计。它不作为完成或验收依据。 |

`dsh.status` 与顶层 `status` 表示不同概念：前者表示管理回合是否在协议层完成，后者表示
任务产物是否通过独立验收。

## `accepted` 与 `rejected`

`accepted` 表示全部验收项通过，并满足 run id、agent、回执和产物路径等一致性校验。

`rejected` 表示 DSH 已正常结束管理和验收流程，但任务没有满足全部要求。它仍然返回完整
`TaskResult`，不会抛出 `ConductorError`。例如，拒绝结论中的关键字段可能是：

```json
{
  "status": "rejected",
  "verdict": {
    "schema_version": 2,
    "run_id": "20260914T071530.123456Z-a1b2c3d4e5f6",
    "status": "rejected",
    "agent": "codex",
    "attempts": 2,
    "artifacts": ["hello.txt"],
    "checks": [
      {
        "criterion_id": "exact-content",
        "criterion": "文件内容必须完全匹配",
        "method": "比较文件 bytes",
        "evidence": "实际内容缺少末尾换行",
        "passed": false
      }
    ],
    "summary": "文件已创建，但内容未完全满足要求",
    "remaining_issues": [
      "hello.txt 缺少末尾换行"
    ]
  }
}
```

上面只展示差异相关字段；真实返回仍包含[顶层字段](#顶层字段)中列出的完整对象。

worker 未生成合法 receipt/result.md 也可以 rejected，DSH 应在停止 worker 后写明屏幕和实际文件
证据。恢复次数不计入 verdict.attempts；该字段仍是业务委派轮数。恢复最多 5 次，具体计数、
选择和停止原因见 state_directory 下的 supervision.json、supervision.jsonl、observations/。
`worker_result` 只在报告存在时返回，rejected 下的报告可能不完整，不能据此判断成功。

## 错误 JSON

Python SDK 遇到 DSH 启动失败、超时、协议未完成或结果文件不合法时会抛出
`ConductorError`，不会返回 `TaskResult`。异常本身也可序列化：

```python
from conductor import Conductor, ConductorError

try:
    result = Conductor("/path/to/project").run(prompt)
except ConductorError as exc:
    error_payload = exc.to_json()
```

CLI 会捕获这类异常，并在 stdout 输出如下 JSON：

```json
{
  "schema_version": 1,
  "status": "error",
  "error": "DSH failed: DSH turn timed out",
  "run_id": "20260914T071530.123456Z-a1b2c3d4e5f6",
  "state_directory": "/home/user/projects/demo/.dsh-conductor/runs/20260914T071530.123456Z-a1b2c3d4e5f6"
}
```

| 字段 | JSON 类型 | 必需 | 含义 |
|---|---|---:|---|
| `schema_version` | integer | 是 | 错误对象 schema 版本，当前固定为 `1`。 |
| `status` | string | 是 | 固定为 `error`。 |
| `error` | string | 是 | 供调用方诊断的错误消息。 |
| `run_id` | string | 否 | 如果错误发生前已创建运行状态，则返回本次 run id。 |
| `state_directory` | string | 否 | 如果错误发生前已创建运行状态，则返回运行记录目录。 |

工作目录无效、配置参数非法等发生在运行状态创建前的错误，通常不会包含 `run_id` 和
`state_directory`。因此调用方必须按可选字段处理，不能用 `null` 判断。

总超时或 DSH 异常时 SDK 会停止身份匹配的本 run worker 并保留 sdk-stop.json 和末次历史快照，
不会伪造 rejected verdict；错误 JSON 与业务拒绝仍明确区分。keep_session 只保留正常验收结束的会话。

## 实时事件 `RunEvent`

传给 `Conductor.run(prompt, on_event=...)` 的回调会实时收到 `RunEvent`。如果需要 JSON，
可在回调中调用 `event.to_json()`：

```json
{
  "elapsed_seconds": 15.284,
  "source": "codex",
  "kind": "worker_output",
  "message": "Running python3.13 -m unittest discover -v"
}
```

| 字段 | JSON 类型 | 必需 | 含义 |
|---|---|---:|---|
| `elapsed_seconds` | number | 是 | 从本次 SDK 进度报告启动后经过的秒数，保留三位小数。 |
| `source` | string | 是 | 事件来源，当前包括 `conductor`、`dsh`、`claude` 和 `codex`。 |
| `kind` | string | 是 | 事件类别，例如 `run_start`、`state`、`turn_start`、`message`、`tool_call`、`tool_result`、`heartbeat`、`worker_output`、`turn_end` 或 `run_end`。 |
| `message` | string | 是 | 适合显示的单行进度文字。调用方不应解析它来判断完成或验收。 |
| `raw` | object | 否 | 对应的原始 DSH 协议事件。worker 屏幕日志、心跳和 conductor 自身事件通常不包含该字段。 |

`RunEvent` 是可观测性数据，不会追加到最终 `TaskResult` JSON。回调异常也不会改变任务
执行和最终验收结果。

监督另会发出 `supervision_needs_attention`、`supervision_recovery`、`supervision_choice`、
`supervision_receipt` 和 `supervision_stopped` 等事件；raw 携带相应审计记录，恢复消息包含累计次数。
关闭 worker_log 不影响这些事件。SDK 心跳默认计入活动时钟，持续心跳会阻止 300 秒静默超时，
但不影响其他诊断及总超时；可通过 sdk_heartbeat_counts_as_activity=False 排除。

## 消费建议

调用方应先检查顶层 `schema_version`，再根据 `status` 分支处理：

```python
payload = result.to_json()

if payload["schema_version"] != 1:
    raise RuntimeError("不支持的返回格式版本")

match payload["status"]:
    case "accepted":
        publish_artifacts(payload["verdict"]["artifacts"])
    case "rejected":
        report_issues(payload["verdict"]["remaining_issues"])
    case other:
        raise RuntimeError(f"未知任务状态: {other}")
```

不要用 `dsh.final_text`、实时日志文字或 tmux 屏幕稳定状态推断任务是否成功。最终业务结论
只读取顶层 `status` 和结构化 `verdict`。
