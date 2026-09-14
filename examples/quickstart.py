#!/usr/bin/env python3.13
"""Run a complete conductor example in a temporary workspace."""

from __future__ import annotations

import tempfile
from pathlib import Path

from conductor.cli import main


with tempfile.TemporaryDirectory(prefix="dsh-conductor-example-") as directory:
    raise SystemExit(
        main(
            [
                "run",
                "--workspace",
                str(Path(directory)),
                "--agent",
                "claude",
                "--task",
                "Create hello.txt containing Hello Conductor followed by one newline.",
                "--verify",
                "hello.txt exists and its bytes are exactly b'Hello Conductor\\n'.",
            ]
        )
    )
