"""定位并准备 conductor 随包提供的 DSH skills。

默认把 skill 放在当前 workspace 的 ``.dsh/skills``，让 DSH 只在本项目中发现它们。
"""

from __future__ import annotations

import os
import shutil
import sysconfig
from pathlib import Path

from .dsh import DshError
from .models import AgentKind
from .lifecycle import Budget

REPO_ROOT = Path(__file__).resolve().parent.parent


def dsh_home(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser().resolve()


def _skill_roots() -> tuple[Path, ...]:
    configured = os.environ.get("DSH_CONDUCTOR_SKILLS")
    roots = [Path(configured).expanduser()] if configured else []
    roots.extend(
        [
            REPO_ROOT / "skills",
            # pip --target 会把 data-files 放在目标目录，而不是当前解释器的 data 目录。
            Path(__file__).resolve().parent.parent / "share" / "dsh-conductor" / "skills",
            Path(sysconfig.get_path("data")) / "share" / "dsh-conductor" / "skills",
        ]
    )
    return tuple(dict.fromkeys(path.resolve() for path in roots))


def source_skill(kind: AgentKind) -> Path:
    for root in _skill_roots():
        path = root / kind.skill_name
        if (path / "SKILL.md").is_file() and (path / "scripts" / kind.script_name).is_file():
            return path
    searched = ", ".join(str(path / kind.skill_name) for path in _skill_roots())
    raise DshError(f"cannot find bundled skill {kind.skill_name}; searched: {searched}")


def workspace_skill_root(workspace: Path) -> Path:
    """返回 DSH 项目级 skill 根目录。"""

    return workspace.expanduser().resolve() / ".dsh" / "skills"


def _remove_path(path: Path) -> None:
    """删除待覆盖的文件、目录或软链。"""

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def prepare_workspace_skills(workspace: Path, *, budget: Budget | None = None) -> Path:
    """把随包 skill 直接覆盖到 workspace 的项目级目录并返回该目录。"""

    root = workspace_skill_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    # 先解析全部源目录，避免中途缺文件时只覆盖了一部分 skill。
    sources = {kind: source_skill(kind) for kind in AgentKind}
    for kind, source in sources.items():
        if budget is not None:
            budget.check()
        target = root / kind.skill_name
        # 直接删除旧版本，避免源目录删掉文件后目标残留旧内容。
        _remove_path(target)
        def copy_file(src: str, dst: str) -> str:
            if budget is not None:
                budget.check()
            return shutil.copy2(src, dst)
        shutil.copytree(source, target, copy_function=copy_file)
    return root


def available_workspace_agent_skills(workspace: Path) -> tuple[dict[AgentKind, Path], set[AgentKind]]:
    """检查项目级 skill，并返回可用 worker。"""

    root = workspace_skill_root(workspace)
    scripts: dict[AgentKind, Path] = {}
    available: set[AgentKind] = set()
    for kind in AgentKind:
        path = root / kind.skill_name
        script = path / "scripts" / kind.script_name
        if not (path / "SKILL.md").is_file() or not script.is_file():
            continue
        scripts[kind] = script
        if shutil.which(kind.value):
            available.add(kind)
    return scripts, available
