#!/usr/bin/env python3.13
"""使用 SDK 发起一次包含任务、验收标准和 agent 偏好的运行。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from conductor import Conductor, ConductorConfig


with tempfile.TemporaryDirectory(prefix="dsh-conductor-example-") as directory:
    client = Conductor(
        Path(directory),
        ConductorConfig(),
    )
    result = client.run(
        """
        请使用 Claude Code 完成以下任务：创建 hello.txt，内容为 Hello Conductor 加一个换行。

        验收标准：hello.txt 必须存在，且读取 bytes 的结果恰好是 b'Hello Conductor\\n'。
        """,
        on_event=lambda event: print(event.format()),
    )
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
