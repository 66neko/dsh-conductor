"""稳定的 SDK 错误协议；底层组件在知道原因的位置分类。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import CleanupReport
    from .sdk import TaskResult


class OperationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "internal_error",
                 phase: str = "preparation", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.details = dict(details or {})

    @property
    def timed_out(self) -> bool:
        return self.code == "timeout"

    @property
    def cancelled(self) -> bool:
        return self.code == "cancelled"


class ConductorError(OperationError):
    """运行故障。合法 rejected 仍通过 TaskResult 返回。"""

    def __init__(self, message: str, *, code: str = "internal_error", phase: str = "preparation",
                 details: dict[str, Any] | None = None, run_id: str | None = None,
                 state_directory: Path | None = None, cleanup: CleanupReport | None = None,
                 result: TaskResult | None = None) -> None:
        super().__init__(message, code=code, phase=phase, details=details)
        self.run_id = run_id
        self.state_directory = state_directory
        self.cleanup = cleanup
        self.result = result
        self.report: str | None = None
        self.report_file: Path | None = None
        self.report_warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": 1, "status": "error", "error": str(self),
            "code": self.code, "phase": self.phase, "timed_out": self.timed_out,
            "cancelled": self.cancelled, "details": self.details,
        }
        if self.run_id is not None:
            value["run_id"] = self.run_id
        if self.state_directory is not None:
            value["state_directory"] = str(self.state_directory)
        if self.cleanup is not None:
            value["cleanup"] = self.cleanup.to_json()
        if self.result is not None:
            value["result"] = self.result.to_json()
        if self.report is not None:
            value["report"] = self.report
            if self.report_file is not None:
                value["report_file"] = str(self.report_file)
            if self.report_warnings:
                value["report_warnings"] = list(self.report_warnings)
        return value
