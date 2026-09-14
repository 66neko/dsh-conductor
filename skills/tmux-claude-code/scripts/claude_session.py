#!/usr/bin/env python3.13
"""tmux-claude-code skill 的仓库内入口。"""

from __future__ import annotations

import sys
from pathlib import Path


def _repository_root() -> Path:
    # skill 通过软链安装，必须从解析后的脚本路径向上定位唯一源码仓库。
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "conductor").is_dir():
            return parent
    raise RuntimeError("cannot locate the dsh-conductor package from this skill")


sys.path.insert(0, str(_repository_root()))

from conductor.agents.claude import main  # noqa: E402

raise SystemExit(main())
