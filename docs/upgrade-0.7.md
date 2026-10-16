# 升级到 0.7.0

本文面向从 0.6.x（及更早版本）升级的 SDK / CLI 调用方。0.7.0 是**破坏性版本**：验收语义改变，
`plan` 与 `verdict` 删除了若干字段。调用接口（`Conductor`、`ConductorConfig`、`run()`、`TaskResult`、
`ConductorError`、`RunEvent`、CLI 参数与退出码）保持不变。

## 1. 一句话说明

0.7.0 起，DSH 不再检查工作区、不再运行命令，**只读取 worker 的 `result.md`，判断报告是否完成用户
prompt 的全部目标**。因此 plan 不再拆分验收项，verdict 不再包含逐项检查和产物列表。

## 2. 快速判断能否直接升级

在调用方项目根目录执行：

```bash
grep -rnE "acceptance_criteria|implementation_steps|\.checks|\[.checks.\]|artifacts|criterion|VerificationCheck|AcceptanceCriterion|Verdict\.load|ExecutionPlan\.load|schema_version" --include=*.py .
```

- **无命中**，且不依赖“accepted 即产物已被独立验证”：可以直接升级。
- **有命中**：按第 4～6 节逐项修改。
- **依赖 accepted 做部署、发布或合并**：先阅读第 3 节并补充调用方自己的产物检查。

## 3. 语义变化（最重要）

| | 0.6.x | 0.7.0 |
|---|---|---|
| DSH 验收对象 | 工作区产物，亲自读文件、运行命令 | 仅 worker 的 `result.md` |
| `accepted` 含义 | 全部计划验收项经 DSH 独立验证通过 | DSH 判定最后一轮 `result.md` 覆盖 prompt 全部目标 |
| SDK 事实校验 | 回执 token、产物路径存在、验收项全覆盖、全部 passed | 回执 token、最后一轮 `result.md` 非空、回执为 `ready_for_verification` |
| 产物存在性 | accepted 时 SDK 检查 `artifacts` 都存在 | 不检查 |

`accepted` 现在等价于“worker 的报告声称并说明完成了全部目标”。worker 在报告中误报完成时，仍可能
得到 accepted。**依赖 accepted 自动部署、发布或合并的调用方，必须自行检查产物**，例如：

```python
result = Conductor(workspace).run(prompt)
if result.accepted:
    # 0.7.0 不再验证工作区；由调用方确认关键产物。
    target = workspace / "dist" / "app.tar.gz"
    if not target.is_file():
        raise RuntimeError("报告声称完成，但产物缺失")
```

需要保留旧的“DSH 独立验收工作区”行为时，请固定在 `dsh-conductor<0.7`。

## 4. `plan` 变化

删除 `implementation_steps` 和 `acceptance_criteria`。保留字段：`schema_version`（仍为 1）、`run_id`、
`agent`、`agent_reason`、`task_summary`。

```python
# 0.6.x
for step in result.plan.implementation_steps: ...
for item in result.plan.acceptance_criteria: print(item.id, item.description)

# 0.7.0：两个属性不存在（AttributeError）。需要展示任务时使用摘要：
print(result.plan.task_summary)
```

JSON 调用方：`payload["plan"]["acceptance_criteria"]` 会 `KeyError`，改用 `.get(...)` 或移除。
新版加载旧运行目录里的 plan 时会忽略这两个多余字段，不报错。

## 5. `verdict` 变化

`schema_version` 从 **2 变为 3**，删除 `checks` 与 `artifacts`。保留字段：

| 字段 | 0.7.0 来源 |
|---|---|
| `schema_version` | 固定 3 |
| `run_id` | SDK 从 plan 填入 |
| `status` | DSH 写入：`accepted` / `rejected` |
| `agent` | SDK 从 plan 填入 |
| `attempts` | SDK 推导：监督登记轮次与最后一个落盘 receipt 轮次的较大者 |
| `summary` | DSH 写入 |
| `remaining_issues` | DSH 写入；accepted 必须为空，rejected 至少一项 |

常见迁移：

```python
# 0.6.x：发布产物
for path in result.verdict.artifacts:
    publish(workspace / path)
# 0.7.0：不再有 artifacts。由调用方按任务约定定位产物，或在 prompt 中要求 worker
# 在 result.md 中列出路径，再读取 result.worker_result 自行解析。

# 0.6.x：展示逐项检查
for check in result.verdict.checks:
    print(check.criterion_id, check.passed, check.evidence)
# 0.7.0：改为展示总结与剩余问题
print(result.verdict.summary)
for issue in result.verdict.remaining_issues:
    print("-", issue)

# 0.6.x：版本断言
assert payload["verdict"]["schema_version"] == 2
# 0.7.0
assert payload["verdict"]["schema_version"] == 3
```

`attempts` 仍然存在，但不再由 DSH 自报。只要依赖它统计返工轮数，含义不变。

## 6. 删除或变更的内部 API

以下名称不在 `conductor` 顶层导出中，但直接 import `conductor.models` 的代码会受影响：

- 删除 `conductor.models.AcceptanceCriterion`、`conductor.models.VerificationCheck`。
- `Verdict(...)` 构造参数变为
  `(schema_version, run_id, status, agent, attempts, summary, remaining_issues)`。
- `ExecutionPlan(...)` 构造参数变为 `(schema_version, run_id, agent, agent_reason, task_summary)`。
- `Verdict.load()` 删除 `max_attempts`、`workspace` 参数，新增可选 `registered_attempts`；
  `expected_receipts` 与 `budget` 不变。
- 新增 `conductor.models.registered_attempts()` 与 `conductor.models.VERDICT_SCHEMA_VERSION`。

测试中手工构造 `TaskResult` 的写法示例：

```python
from conductor.models import AgentKind, ExecutionPlan, Verdict

plan = ExecutionPlan(1, "run-1", AgentKind.CLAUDE, "用户指定", "创建 hello.py")
verdict = Verdict(3, "run-1", "accepted", AgentKind.CLAUDE, 1, "报告覆盖全部目标", ())
```

## 7. CLI 变化

- `conductor run` 参数、stdout 单一 JSON、退出码（0 / 1 / 124 / 130 / 143）均不变。
- `conductor validate-verdict` 校验通过后**自动把 `--verdict` 指定的临时文件 rename 为正式
  `verdict.json`**，输出新增 `verdict_file` 字段。它主要供 DSH 使用；若调用方脚本用它只做校验，
  请注意通过后原 `.tmp` 文件会消失。
- 该命令删除了对 `max_attempts`、产物路径和验收项覆盖的校验，原因见第 3 节。

## 8. 不变的部分

- `Conductor(workspace, config)`、`run(prompt, on_event, *, cancel_event)`。
- `ConductorConfig` 全部字段与默认值，包括 `max_attempts`、`include_report`、`keep_session`、超时与心跳。
- `TaskResult` 字段与 `to_json()` 顶层字段，顶层 `schema_version` 仍为 1，`accepted` 字段仍存在。
- `ConductorError` 的 `code`/`phase`、`report` 系列字段、清理与取消语义。
- `RunEvent` 回调、worker 日志、tmux 隔离、恢复预算、`cleanup_run`、`doctor`、`install-skills`、`show`。
- worker receipt schema 仍为 1；`result.md` 仍须非空 UTF-8。
- `include_report` 报告格式：执行记录 + 各轮 `result.md` 正文 + 验收结果。

## 9. 升级步骤

1. 确认没有正在执行的 run。新版不接受旧 run 目录中未完成的 v2 verdict；已完成的历史运行目录不受影响，
   `cleanup_run` 可继续使用。
2. 执行第 2 节的 grep，按第 4～7 节修改命中代码。
3. 评估第 3 节：对依赖 accepted 自动执行后续动作的流程补充产物检查。
4. 建议在 prompt 中要求 worker 在 `result.md` 中逐条说明每个目标的完成情况，验收会更稳定。
5. 升级：`python3.13 -m pip install --upgrade "dsh-conductor==0.7.0"`。
6. 执行 `python3.13 -m conductor doctor`，再用一个小任务验证结果。
7. 需要回退时：`python3.13 -m pip install "dsh-conductor<0.7"`。
