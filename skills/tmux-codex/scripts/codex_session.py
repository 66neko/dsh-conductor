#!/usr/bin/env python3.13
"""tmux-codex skill 的仓库内入口。"""

from __future__ import annotations

import sys
from pathlib import Path


def _repository_root() -> Path | None:
    # 源码软链从仓库导入；wheel 安装后的 data-file 入口直接使用已安装包。
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "conductor").is_dir():
            return parent
    return None


if repository_root := _repository_root():
    sys.path.insert(0, str(repository_root))

from conductor.agents.codex import main  # noqa: E402

raise SystemExit(main())
