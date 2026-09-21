# 完整任务报告与验收报告（0.5.2）

完整报告模式把本次运行所有实际轮次的 worker 报告、登记的内部子任务报告正文，以及 DSH
独立验收结果合并成一份 Markdown。正文直接读取文件，不由模型重新概括，也不从 tmux 历史拼接。
功能默认关闭；需要完整正文的调用方显式开启。

## Python SDK

```python
from conductor import Conductor, ConductorConfig, ConductorError

client = Conductor("/path/to/workspace", ConductorConfig(include_report=True))
try:
    result = client.run("请使用 Codex 实现订单汇总。验收标准：单元测试通过。")
except ConductorError as exc:
    # 超时、取消、协议或结果校验失败继续使用既有错误协议。
    print(exc.code, str(exc))
    if exc.report is not None:
        print(exc.report)
    payload = exc.to_json()
else:
    print(result.report)
    print("报告文件：", result.report_file)
    for warning in result.report_warnings:
        print("报告收集提示：", warning)
    payload = result.to_json()
    # 判断业务成功仍读取这些字段；报告正文和报告是否齐全不能替代验收。
    print(result.accepted, payload["status"])
```

`report` 是字符串正文，`report_file` 是可选 Path。报告形成后保存在返回对象中；重复调用
`to_json()` 不再读取文件，随后删除或修改运行目录不会改变已经返回的正文快照。

## CLI 与 quickstart

```bash
python3.13 -m conductor run \
  --workspace /path/to/workspace \
  --prompt '请使用 Codex 实现订单汇总。验收标准：单元测试通过。' \
  --include-report > result.json

python3.13 -c 'import json; print(json.load(open("result.json"))["report"])'

python3.13 examples/quickstart.py --agent codex --include-report
python3.13 examples/quickstart.py --agent claude --include-report --stress-report
```

CLI stdout 仍然只有一个 JSON 对象，进度仍写 stderr；JSON 内的 `report` 是含换行的完整字符串。
quickstart 会在原有检查之外打印合并报告，并将正文一起保存到 quickstart-result.json。
`conductor show --workspace ...` 可查看已落盘的 `report_file` 路径。

## 返回字段与两部分内容

| 新字段 | Python / JSON 类型 | 含义 |
|---|---|---|
| report | str / string | 全部已收集正文与任务验收报告组成的 Markdown |
| report_file | Path 或 None / string（缺失时省略） | 本轮运行目录 report.md；写入失败或没有剩余预算时不提供 |
| report_warnings | tuple[str, ...] / array[string]（为空时省略） | 缺失、未完成、身份不匹配、读取/持久化失败等提示 |

报告固定有两个主要部分：

1. **所有任务结果报告**：任务概述；按轮次排列的每份 result.md；该轮清单登记的全部子任务正文。
   返工前报告标记为历史轮次，最后一轮也不凭 worker 自述宣告通过。保留原始文本的内容与顺序，
   不设行数截断、不把报告正文替换成路径，不展开普通测试日志等任意附件。
2. **任务验收报告**：从已校验的 verdict 生成最终结论、summary、每项 criterion/method/evidence/passed、
   artifacts 与 remaining_issues。有运行错误但没有可信 verdict 时明确写“未形成可返回的有效验收结论”。

开启后在正常返回的 JSON 中添加上述字段；关闭时三个字段均省略。`worker_result` 继续表示最后一轮
result.md 的路径，`dsh.final_text` 继续保存 DSH 原始最后回复。run() 签名、回调、退出码和各协议
schema 版本不变，顶层为 1，plan=1、verdict=2、receipt=1。新增 dataclass 字段位于末尾并提供默认值。

## 内部子任务报告清单

DSH 每次 run 仍只选择一个 Claude 或 Codex worker；这里的子任务清单描述该 worker 在自身执行中
委派的任务，不新增 SDK 层多个 worker 的调度能力。所有返工仍沿用原来的 tmux 会话。

开启模式后管理提示词、任务文件要求和控制器交接指令会要求 worker 维护每轮的
`subtask-reports.json`。没有内部子任务也必须写入空 subtasks 数组，以区别“没有子任务”和“缺少清单”。

```json
{
  "schema_version": 1,
  "receipt_token": "本轮 request 预定的 token",
  "subtasks": [
    {
      "id": "implementation",
      "parent_id": null,
      "agent": "claude",
      "title": "实现功能",
      "status": "completed",
      "report_file": "subtasks/implementation.md"
    },
    {
      "id": "tests",
      "parent_id": "implementation",
      "agent": "codex",
      "title": "验证边界条件",
      "status": "completed",
      "report_file": "subtasks/tests.md"
    }
  ]
}
```

清单须在子任务委派前登记、过程中更新，所有后代平铺在同一个 subtasks 数组，父任务先登记。
id 是 1–64 位字母数字、下划线或连字符且以字母数字开头，本轮内唯一。直接子任务 parent_id 为 null；
其他项引用此前登记的父任务。agent 仅为 claude/codex，status 为 running/completed/blocked/failed，
仅表示 worker 自报状态。每个 report_file 是本轮 subtasks/ 内唯一相对路径，文件须是非空 UTF-8 正文。
绝对路径、越界路径、越界符号链接、管道/设备文件、重复项及不匹配的 token 都不能作为正常子报告收集。

提交 receipt 前先原子保存 result.md、全部子报告和清单。主 result.md 保留完整主任务回答，子报告
通过清单引用以避免正文重复；SDK 再逐个展开原文。DSH 仍要独立读取文件并执行验收。

此清单是 worker 声明的报告目录，SDK 能核对已登记项，不能发现 worker 隐瞒的、未登记的内部委派，
也不能恢复历史运行中从未保存的子报告。不要把“无 report_warnings”解释为对子代理行为的独立审计。

## 失败、缺失与预算

- rejected 是正常 TaskResult：已有各轮正文仍合并，缺失报告明确列出，不把缺失正文当作成功信号。
- 子报告/清单缺失、编码错误或损坏会产生 report_warnings，不改写既有业务 verdict。
  原有 accepted 所需的 receipt/result.md 校验仍然执行，不因开启报告模式而放宽。
- 合并后的 report.md 原子写入。写入失败时仍返回已收集的内联正文，report_file 省略并产生提示。
- 正常报告读取与写入共用 run 的剩余执行预算，超时和取消仍走原错误路径。
- 执行失败时先完成资源清理，再在剩余清理预算内尽力收集报告；不延长总时限、不覆盖原始错误。
  没有剩余预算时返回说明收集未完成的报告，而不假装已经收齐正文。尚未创建运行目录的错误没有报告。
- 清理失败但已有可信业务结果时，原 exc.result 继续保留；其报告也保留。报告落盘路径不是工作区业务产物，
  不加入 verdict.artifacts，不影响成功判断。

正文会增加 JSON 的体积与调用方内存占用，大小与报告总量相关。显示层可以折叠或分页，但应保留完整 payload；
需要完整报告的客户端读取 report，旧客户端继续读取原字段即可。
