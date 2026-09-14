"""定位、安装并检查 conductor 随包提供的 DSH skills。"""

from __future__ import annotations

import os
import shutil
import sysconfig
import time
from pathlib import Path

from .dsh import DshError
from .models import AgentKind

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


def installed_skill(kind: AgentKind, home: Path) -> Path:
    path = home / "skills" / kind.skill_name
    script = path / "scripts" / kind.script_name
    if not path.is_symlink() or not script.is_file():
        raise DshError(
            f"{kind.skill_name} is not installed as a valid symlink; "
            "run `conductor install-skills`"
        )
    return path.resolve()


def install_link(source: Path, target: Path) -> tuple[str, str | None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve(strict=False) == source:
        return "unchanged", None
    backup: str | None = None
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        # 真实目录可能包含用户内容，先备份再建立唯一来源的软链。
        backup_path = target.with_name(f"{target.name}.backup-{int(time.time())}")
        shutil.move(target, backup_path)
        backup = str(backup_path)
    target.symlink_to(source, target_is_directory=True)
    return "installed", backup


def available_agent_skills(home: Path) -> tuple[dict[AgentKind, Path], set[AgentKind]]:
    """返回可生成命令的脚本路径，以及当前真正可委派的 agent 集合。"""

    scripts: dict[AgentKind, Path] = {}
    available: set[AgentKind] = set()
    for kind in AgentKind:
        source = source_skill(kind)
        scripts[kind] = source / "scripts" / kind.script_name
        try:
            installed = installed_skill(kind, home)
        except DshError:
            continue
        scripts[kind] = installed / "scripts" / kind.script_name
        if shutil.which(kind.value):
            available.add(kind)
    return scripts, available
