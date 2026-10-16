# Changelog

All notable changes to this project will be documented in this file.

## [0.7.0] - 2026-10-16

### ⚠️ Breaking changes

This release changes acceptance semantics and removes fields from `plan` and `verdict`.
Read the full upgrade guide before upgrading: [docs/upgrade-0.7.md](https://github.com/66neko/dsh-conductor/blob/v0.7.0/docs/upgrade-0.7.md).

- **Acceptance semantics.** DSH no longer inspects the workspace or runs commands. It only reads the worker's `result.md` and decides whether the report covers every goal in the caller prompt. `accepted` now means "the final `result.md` reports all prompt goals as done", not "DSH independently verified the workspace". Callers that deploy, publish or merge on `accepted` must check artifacts themselves.
- **`plan` fields removed:** `implementation_steps`, `acceptance_criteria`. Remaining: `schema_version` (still 1), `run_id`, `agent`, `agent_reason`, `task_summary`. Extra fields in old plan files are ignored.
- **`verdict` schema 2 → 3; fields removed:** `checks`, `artifacts`. Remaining: `schema_version`, `run_id`, `status`, `agent`, `attempts`, `summary`, `remaining_issues`. v2 verdict files are rejected.
- **SDK no longer checks** that verdict artifacts exist, that every acceptance criterion is covered, or that all checks passed.
- **Internal API (`conductor.models`):** `AcceptanceCriterion` and `VerificationCheck` removed; `ExecutionPlan(...)` and `Verdict(...)` constructor arguments changed; `Verdict.load()` drops `max_attempts` and `workspace` and gains optional `registered_attempts`.
- **`conductor validate-verdict`** now renames a valid `--verdict` temporary file to the final `verdict.json`, and its output adds `verdict_file`.

### Changed

- DSH writes only `status`, `summary` and `remaining_issues`. The SDK fills `run_id` and `agent` from the plan, and derives `attempts` from `supervision.json` and on-disk receipts (the larger of the two).
- `accepted` still requires a valid token-bound receipt for the final attempt, a non-empty UTF-8 `result.md`, and `ready_for_verification` receipt status; `rejected` still requires at least one remaining issue.
- The final rename to `verdict.json` is done by `validate-verdict`, so a run no longer fails because DSH validated the verdict but forgot to rename it.
- DSH manager contract (`conductor/prompt.py`) is shorter: section 3 is report-only acceptance, section 4 marks the verdict as mandatory with a three-field schema. Task files ask workers to state how each prompt goal was completed in `result.md`.
- Both skill `SKILL.md` files, README, AGENTS.md and `docs/` (architecture, lifecycle, result-json, full-report) updated for the new flow.

### Added

- `docs/upgrade-0.7.md`: complete upgrade guide with grep checklist, before/after code, unchanged surface and rollback steps.
- `conductor.models.registered_attempts()` and `conductor.models.VERDICT_SCHEMA_VERSION`.
- CLI test covering `validate-verdict` rename only on a valid verdict.

### Unchanged

- `Conductor`, `ConductorConfig` (all fields and defaults), `run(prompt, on_event, *, cancel_event)`, `TaskResult` and top-level JSON (`schema_version` 1, `status`, `accepted`), `ConductorError` codes/phases, `RunEvent`, CLI `run` arguments, stdout JSON and exit codes, receipt schema 1, `include_report` format, tmux isolation, recovery budget, cleanup and cancellation.

### Upgrade recommendations

1. Make sure no run is in progress; finished run directories stay usable with `cleanup_run`.
2. Search caller code for `acceptance_criteria`, `implementation_steps`, `checks`, `artifacts`, `criterion`, `schema_version`, `Verdict.load`, `ExecutionPlan.load` and migrate hits as shown in the guide.
3. Add your own artifact checks wherever `accepted` triggers automated deployment or publishing.
4. Ask workers in your prompt to list every goal and its completion status in `result.md`.
5. Upgrade with `pip install --upgrade "dsh-conductor==0.7.0"`, run `python3.13 -m conductor doctor`, then a small task. Roll back with `pip install "dsh-conductor<0.7"`.

### Validation

- All 148 unit tests pass; `compileall` clean.
- Live DSH 0.1.6-alpha.2 + Claude Code run: 1 attempt, accepted in 75 s, verdict validated and renamed on the first try; DSH used about 5 s from receipt to final verdict.

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
