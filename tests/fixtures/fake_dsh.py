#!/usr/bin/env python3.13
"""协议测试使用的最小 JSON-RPC DSH fixture。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import subprocess
import signal
from pathlib import Path


def emit(value: object) -> None:
    print(json.dumps(value), flush=True)


if os.environ.get("FAKE_DSH_EXIT_EARLY"):
    print("fixture startup failure", file=sys.stderr, flush=True)
    raise SystemExit(17)


if os.environ.get("FAKE_DSH_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if ready := os.environ.get("FAKE_DSH_READY_FILE"):
    Path(ready).write_text(str(os.getpid()))

for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    method = request["method"]
    if method == "initialize":
        time.sleep(float(os.environ.get("FAKE_DSH_INIT_DELAY", "0")))
        if os.environ.get("FAKE_DSH_RPC_ERROR"):
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -1, "message": "fixture RPC failure"}})
            continue
        if (
            os.environ.get("FAKE_DSH_CHECK_PERMISSION")
            and os.environ.get("DSH_PERMISSION_MODE") != "danger-full-access"
        ):
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -1, "message": "bad permission"}})
        elif (
            os.environ.get("FAKE_DSH_CHECK_SKILL_DIR")
            and not Path(os.environ.get("DSH_BUNDLED_SKILL_DIR", "")).is_dir()
        ):
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -1, "message": "bad skill dir"}})
        elif (
            os.environ.get("FAKE_DSH_CHECK_PYTHONPATH")
            and str(Path(__file__).resolve().parents[2]) not in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        ):
            emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -1, "message": "bad python path"}})
        else:
            emit({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
        if os.environ.get("FAKE_DSH_FREEZE_STDIN"):
            time.sleep(60)
    elif method == "session/prompt":
        time.sleep(float(os.environ.get("FAKE_DSH_PROMPT_DELAY", "0")))
        if os.environ.get("FAKE_DSH_EXIT_WITH_CHILD"):
            child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('child ready', flush=True); time.sleep(60)"], stdout=sys.stderr)
            Path(os.environ["FAKE_DSH_CHILD_PID"]).write_text(str(child.pid))
            time.sleep(0.1)
            raise SystemExit(23)
        if os.environ.get("FAKE_DSH_INVALID_FRAME"):
            print("{invalid JSON", flush=True)
        if os.environ.get("FAKE_DSH_SDK"):
            prompt = (((request.get("params") or {}).get("contentBlocks") or [{}])[0]).get("text", "")
            request_match = re.search(r"`([^`]+/request\.json)`", prompt)
            if request_match:
                request_path = Path(request_match.group(1))
                request_data = json.loads(request_path.read_text(encoding="utf-8"))
                selected = request_data["agents"][0]
                # fixture 通过 Claude 候选写入最小可验证产物，模拟 DSH 的完整落盘链。
                if os.environ.get("FAKE_DSH_AGENT") == "codex":
                    selected = request_data["agents"][1]
                attempt = selected["attempts"][0]
                workspace = Path(request_data["workspace"])
                (workspace / "fixture.txt").write_text("ok\n", encoding="utf-8")
                if not os.environ.get("FAKE_DSH_MISSING_RESULT"):
                    Path(attempt["result_file"]).write_text(
                        "已创建 fixture.txt，完整检查结果：内容为 ok。\n", encoding="utf-8"
                    )
                receipt = {
                    "schema_version": 1,
                    "token": attempt["token"],
                    "status": "ready_for_verification",
                    "summary": "fixture worker finished",
                }
                Path(attempt["receipt_file"]).write_text(json.dumps(receipt), encoding="utf-8")
                if request_data.get("include_report"):
                    Path(attempt["result_file"]).with_name("subtask-reports.json").write_text(json.dumps({
                        "schema_version": 1, "receipt_token": attempt["token"], "subtasks": [],
                    }), encoding="utf-8")
                    (request_path.parent / "supervision.json").write_text(json.dumps({
                        "schema_version": 1, "run_id": request_data["run_id"], "agent": selected["agent"],
                        "session": selected["session"], "attempt": 1,
                    }), encoding="utf-8")
                plan = {
                    "schema_version": 1,
                    "run_id": request_data["run_id"],
                    "agent": selected["agent"],
                    "agent_reason": "fixture selection",
                    "task_summary": "fixture task",
                    "implementation_steps": ["write fixture artifact"],
                    "acceptance_criteria": [{"id": "criterion-1", "description": "fixture.txt exists"}],
                }
                Path(request_data["plan_file"]).write_text(json.dumps(plan), encoding="utf-8")
                if os.environ.get("FAKE_DSH_WORKER"):
                    from conductor.tmux import TmuxSession
                    runtime_file = request_path.parent / "runtime.json"
                    runtime = json.loads(runtime_file.read_text())
                    command = [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN); print('fixture worker evidence', flush=True); time.sleep(60)"]
                    TmuxSession.create(name=selected["session"], workspace=workspace, agent=selected["agent"],
                                       command=command, socket_path=Path(runtime["tmux_socket"]), runtime_file=runtime_file,
                                       activity_file=request_path.parent / "worker-activity.json")
                    if os.environ.get("FAKE_DSH_EXTRA_WORKER"):
                        other = next(item for item in request_data["agents"] if item != selected)
                        TmuxSession.create(name=other["session"], workspace=workspace, agent=other["agent"],
                                           command=command, socket_path=Path(runtime["tmux_socket"]), runtime_file=runtime_file)
                        time.sleep(0.1)  # 等待两个 worker 都安装忽略 HUP/TERM 的处理器。
                    if marker := os.environ.get("FAKE_DSH_WORKER_READY"):
                        Path(marker).write_text(selected["session"])
                if os.environ.get("FAKE_DSH_HOLD_LOCK"):
                    child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)", str(request_path.parent / "supervision.lock")], stdout=sys.stderr)
                    time.sleep(0.1)
                verdict = {
                    "schema_version": 2,
                    "run_id": request_data["run_id"],
                    "status": "accepted",
                    "agent": selected["agent"],
                    "attempts": 1,
                    "artifacts": ["fixture.txt"],
                    "checks": [{
                        "criterion_id": "criterion-1",
                        "criterion": "fixture.txt exists",
                        "method": "test fixture",
                        "evidence": "fixture.txt exists",
                        "passed": True,
                    }],
                    "summary": "fixture accepted",
                    "remaining_issues": [],
                }
                if os.environ.get("FAKE_DSH_REJECTED"):
                    verdict.update(status="rejected", summary="fixture failed", remaining_issues=["worker unavailable"])
                    verdict["checks"][0]["passed"] = False
                    Path(attempt["receipt_file"]).unlink()
                    Path(attempt["result_file"]).unlink(missing_ok=True)
                Path(request_data["verdict_file"]).write_text(json.dumps(verdict), encoding="utf-8")
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
        if os.environ.get("FAKE_DSH_NO_TURN"):
            continue
        emit(event)
        if os.environ.get("FAKE_DSH_NO_IDLE"):
            emit(turn_end)
            break
        if os.environ.get("FAKE_DSH_ORDER") == "idle-first":
            emit(idle)
            emit(turn_end)
        else:
            emit(turn_end)
            emit(idle)
    elif method == "shutdown":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
        break
