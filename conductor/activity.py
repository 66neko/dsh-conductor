"""tmux pipe-pane 的只读活动计数器；不保存或解释输出正文。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    target = Path(sys.argv[1])
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    total = 0
    try:
        while chunk := os.read(sys.stdin.fileno(), 65536):
            total += len(chunk)
            temporary.write_text(json.dumps({"bytes": total, "last_output_at": time.time()}), encoding="utf-8")
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
