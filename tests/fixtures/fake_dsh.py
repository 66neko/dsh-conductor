#!/usr/bin/env python3.13
"""协议测试使用的最小 JSON-RPC DSH fixture。"""

from __future__ import annotations

import json
import os
import sys


def emit(value: object) -> None:
    print(json.dumps(value), flush=True)


if os.environ.get("FAKE_DSH_EXIT_EARLY"):
    print("fixture startup failure", file=sys.stderr, flush=True)
    raise SystemExit(17)


for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    method = request["method"]
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
    elif method == "session/prompt":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"queued": True}})
        event = {
            "jsonrpc": "2.0",
            "method": "session.event",
            "params": {
                "event": {
                    "type": "assistant/message",
                    "data": {"message": {"content": [{"type": "text", "text": "fixture complete"}]}},
                }
            },
        }
        turn_end = {
            "jsonrpc": "2.0",
            "method": "session.event",
            "params": {"event": {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}},
        }
        idle = {
            "jsonrpc": "2.0",
            "method": "session.status",
            "params": {"status": "idle"},
        }
        emit(event)
        if os.environ.get("FAKE_DSH_ORDER") == "idle-first":
            emit(idle)
            emit(turn_end)
        else:
            emit(turn_end)
            emit(idle)
    elif method == "shutdown":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
        break
