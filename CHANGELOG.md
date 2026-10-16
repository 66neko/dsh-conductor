# Changelog

All notable changes to this project will be documented in this file.

## [0.7.0] - 2026-10-16

本版重构验收流程：DSH 不再检查工作区、不再运行命令，**只读取 worker 的 `result.md`，判断报告是否完成
用户 prompt 的全部目标**。plan 与 verdict 随之精简，DSH 在验收阶段只需写三个字段，缩短验收耗时并消除
“verdict 未落盘导致没有验收报告”的主要成因。本版包含破坏性变更，升级前请阅读完整
[升级指南](https://github.com/66neko/dsh-conductor/blob/v0.7.0/docs/upgrade-0.7.md)。

### ⚠️ 破坏性变更

- **验收语义改变。** `accepted` 由“DSH 已独立验证工作区全部验收项”变为“DSH 判定最后一轮 `result.md`
  报告完成了 prompt 全部目标”。worker 在报告中误报完成时仍可能 accepted；依赖 accepted 自动部署、
  发布或合并的调用方，需要自行检查产物。
- **plan 删除字段：** `implementation_steps`、`acceptance_criteria`。保留 `schema_version`（仍为 1）、
  `run_id`、`agent`、`agent_reason`、`task_summary`。旧 plan 文件中的多余字段会被忽略。
- **verdict schema 由 2 升为 3，删除字段：** `checks`、`artifacts`。保留 `schema_version`、`run_id`、
  `status`、`agent`、`attempts`、`summary`、`remaining_issues`。v2 verdict 文件会被拒绝。
- **SDK 不再校验** verdict 产物路径是否存在、验收项是否全覆盖、各检查项是否全部通过。
- **内部 API（`conductor.models`）：** 删除 `AcceptanceCriterion`、`VerificationCheck`；`ExecutionPlan(...)`
  与 `Verdict(...)` 构造参数改变；`Verdict.load()` 删除 `max_attempts`、`workspace` 参数，新增可选
  `registered_attempts`。
- **`conductor validate-verdict`** 校验通过后会把 `--verdict` 指定的临时文件 rename 为正式
  `verdict.json`，输出新增 `verdict_file` 字段。

### 改进

- DSH 只写 `status`、`summary`、`remaining_issues`；`run_id`、`agent` 由 SDK 从 plan 填入，`attempts`
  取 `supervision.json` 登记轮次与最后一个已落盘 receipt 轮次中的较大者，不再依赖 DSH 自报。
- accepted 仍要求最后一轮 token 回执有效、`result.md` 为非空 UTF-8，且回执状态为
  `ready_for_verification`；rejected 仍要求至少一项 `remaining_issues`。
- 正式 verdict 的 rename 由 `validate-verdict` 完成，不会再出现“校验通过但忘记 rename”导致运行失败。
- DSH 管理契约（`conductor/prompt.py`）精简：第 3 节改为仅报告验收，第 4 节明确 verdict 为必须完成项并
  只要求三字段；任务文件要求 worker 在 `result.md` 中逐条说明 prompt 每个目标的完成情况。
- 两个 skill 的 `SKILL.md`、README、AGENTS.md 以及 `docs/`（architecture、lifecycle、result-json、
  full-report）同步更新。

### 新增

- `docs/upgrade-0.7.md`：完整升级指南，含 grep 自查命令、迁移前后代码对照、不变部分清单与回退方法。
- `conductor.models.registered_attempts()` 与 `conductor.models.VERDICT_SCHEMA_VERSION`。
- CLI 测试：覆盖 `validate-verdict` 仅在 verdict 有效时才 rename。

### 保持不变

`Conductor`、`ConductorConfig`（全部字段与默认值）、`run(prompt, on_event, *, cancel_event)`、`TaskResult`
及顶层 JSON（`schema_version` 1、`status`、`accepted`）、`ConductorError` 的 code/phase、`RunEvent`、
CLI `run` 参数、stdout 单一 JSON 与退出码、receipt schema 1、`include_report` 报告格式、tmux 隔离、
恢复预算、清理与取消语义。

### 升级建议

1. 确认没有正在执行的 run；已结束的历史运行目录不受影响，`cleanup_run` 仍可使用。
2. 在调用方代码中搜索 `acceptance_criteria`、`implementation_steps`、`checks`、`artifacts`、`criterion`、
   `schema_version`、`Verdict.load`、`ExecutionPlan.load`，按升级指南迁移命中的代码。
3. 对以 accepted 触发自动部署或发布的流程，补充调用方自己的产物检查。
4. 在 prompt 中要求 worker 在 `result.md` 中逐条列出每个目标及其完成情况，验收会更稳定。
5. 执行 `pip install --upgrade "dsh-conductor==0.7.0"`，再运行 `python3.13 -m conductor doctor` 和一个
   小任务验证；需要回退时执行 `pip install "dsh-conductor<0.7"`。

### 验证

- 148 个单元测试全部通过，`compileall` 无错误。
- 真实 DSH 0.1.6-alpha.2 + Claude Code 运行：1 轮完成，75 秒 accepted，verdict 首次校验即通过并自动
  rename；DSH 从收到回执到写完 verdict 约 5 秒。

---

## [0.6.3] - 2026-10-15

### Changed

- Worker handoff prompt now asks for a concise `result.md` with four sections: conclusion, modified files, one-line checks, and remaining issues or blockers.
- `result.md` should stay within about 60 lines, must not restate the task file or work process, and retry attempts only describe changes made in that attempt.
- Full logs, command output, long answers, and long code are saved as separate files in the attempt directory and referenced by path from `result.md`.
- DSH contract and both skill `SKILL.md` files use the same concise reporting requirement, which keeps `include_report` output much shorter.

### Validation

- All unit tests pass
- Live Claude Code runs produced 15-line and 23-line `result.md` files following the new structure

---

## [0.6.2] - 2026-10-15

### Fixed

- Add explicit `accepted` boolean field to `TaskResult.to_json()` output for improved CLI JSON consumer ergonomics
- CLI JSON output now includes both `"status": "accepted"` and `"accepted": true` fields
- Eliminates need for downstream consumers to manually check `result["status"] == "accepted"`

### Validation

- All 146 unit tests pass
- Backward compatible: existing `status` field remains unchanged

---

## [0.6.1] - 2026-10-13

### Fixed

- Keep watching while Codex is automatically reconnecting, without treating connection error details or inactivity as ordinary worker failures.
- Support any configured reconnect retry limit (`N/M`), including waiting for the final retry to finish instead of assuming a fixed `5/5` limit.
- Resume the original task through the dedicated `reconnect` command only after automatic retries have failed. Count reconnects and their Enter resubmissions separately from the error recovery budget.
- Preserve the original attempt and receipt token across reconnects, while keeping the run deadline, cancellation, and worker shutdown safeguards.
- Accept bracketed version headings when extracting GitHub Release notes from this changelog.

### Validation

- Add adapter and supervision coverage for configurable retry limits, terminal connection failures, resumed execution, and independent reconnect accounting.
- Exercise reconnect recovery in real tmux sessions with retry limits of 3 and 12.

---

## [0.6.0] - 2026-10-12

### 🎯 Major Refactoring - Streamlined Architecture

This release represents a significant **27% code reduction** while maintaining 100% API compatibility.

### Changed

- **Removed entire subtask reporting mechanism**: Eliminated complex subtask-reports.json and subtasks/ directory requirements, simplifying the worker handoff protocol
- **Streamlined prompt generation** (`conductor/prompt.py`): Reduced from 290 to 225 lines (-22%) by removing redundant explanations and focusing on core contracts
- **Simplified report generation** (`conductor/reports.py`): Reduced from 221 to 159 lines (-28%) with cleaner verification sections
- **Condensed skill documentation**:
  - `skills/tmux-claude-code/SKILL.md`: 145 → 93 lines (-36%)
  - `skills/tmux-codex/SKILL.md`: 145 → 108 lines (-26%)
- **Updated test suite**: Removed 9 subtask-report-related tests, updated assertions to match new concise report format

### Report Format Changes

**Before** (verbose):
```markdown
## 一、所有任务结果报告

### Codex 第 1 轮报告
worker result...

### 子任务 task-1 — Claude Code：具体任务
subtask report...

## 二、任务验收报告

最终结论：通过（accepted）
全部验收通过
```

**After** (concise):
```markdown
## 执行记录

使用 Codex 执行，共 1 轮。

worker result...

## 验收结果

**✓ 通过**

全部验收通过
```

### API Compatibility

✅ **100% backward compatible** - All public APIs unchanged:
- `Conductor`, `ConductorConfig`, `ConductorError`
- `TaskResult` with all fields (`report` content is more concise)
- All configuration parameters including `include_report`

### Migration

**No migration needed.** Existing code continues to work without changes. The only difference is cleaner report output.

### Statistics

- **Total lines removed**: 216 lines (-27%)
- **Code changes**: 499 deletions, 242 insertions
- **Tests**: All 93 tests passing
- **Modules affected**: 4 core files, 2 skill docs, 4 test files

### Documentation

- Added `REFACTOR_SUMMARY.md`: Complete refactoring details
- Added `FURTHER_OPTIMIZATION_ANALYSIS.md`: Future optimization opportunities

---

## [0.5.4] - 2026-10-12

### Fixed
- Verdict self-validation with proper schema checks

---

## [0.5.3] - 2026-10-11

### Added
- Faster quickstart example
- More concise initial reports

---

## [0.5.2] - 2026-10-10

### Added
- Complete task and verification reports
- Enhanced report snapshot functionality

---

## [0.5.1] - 2026-10-09

### Fixed
- tmux quickstart compatibility improvements

---

[0.6.1]: https://github.com/66neko/dsh-conductor/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/yu/dsh-conductor/compare/v0.5.4...v0.6.0
[0.5.4]: https://github.com/yu/dsh-conductor/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/yu/dsh-conductor/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/yu/dsh-conductor/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/yu/dsh-conductor/releases/tag/v0.5.1
