"""由 DSH 管理运行在 tmux 中的编码 agent。"""

from .dsh import DshClient, DshConfig, DshError, RunResult
from .models import AgentKind, ExecutionPlan, Verdict
from .progress import RunEvent, EventCallback
from .runtime import CleanupReport, cleanup_run
from .sdk import Conductor, ConductorConfig, ConductorError, TaskResult
from ._version import __version__

__all__ = [
    "AgentKind",
    "Conductor",
    "CleanupReport",
    "cleanup_run",
    "EventCallback",
    "ConductorConfig",
    "ConductorError",
    "DshClient",
    "DshConfig",
    "DshError",
    "RunResult",
    "RunEvent",
    "TaskResult",
    "ExecutionPlan",
    "Verdict",
    "__version__",
]
