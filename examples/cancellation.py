#!/usr/bin/env python3.13
"""在指定秒数后取消任务，输出结构化结果；运行记录保留供调用方检查。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import Conductor, ConductorConfig, ConductorError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--cancel-after", type=float, default=30)
    args = parser.parse_args()
    cancel = threading.Event()
    timer = threading.Timer(args.cancel_after, cancel.set)
    timer.start()
    try:
        result = Conductor(args.workspace, ConductorConfig(timeout_seconds=600, cleanup_timeout_seconds=5)).run(
            args.prompt, cancel_event=cancel,
            on_event=lambda event: print(event.format(), file=sys.stderr, flush=True),
        )
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
        return 0 if result.accepted else 1
    except ConductorError as exc:
        print(json.dumps(exc.to_json(), ensure_ascii=False, indent=2))
        return 124 if exc.timed_out else 1
    finally:
        timer.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
