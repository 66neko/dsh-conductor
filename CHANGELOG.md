# Changelog

All notable changes to this project will be documented in this file.

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
