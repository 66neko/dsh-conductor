"""进程身份与受管进程组。恢复清理从不只凭裸 PID 发信号。"""

from __future__ import annotations

import os
import signal
from functools import cache
from pathlib import Path
from typing import Any

from .state import atomic_write_json


@cache
def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def process_identity(pid: int) -> dict[str, Any] | None:
    try:
        directory = Path(f"/proc/{pid}")
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]), "sid": int(fields[3]),
                "start_time": fields[19], "uid": directory.stat().st_uid, "boot_id": boot_id(),
                "state": fields[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_process(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    return bool(actual and expected.get("boot_id") and all(
        expected.get(key) == actual.get(key) for key in ("pid", "start_time", "uid", "boot_id")))


def validate_process_record(record: dict[str, Any]) -> None:
    """落盘记录损坏时拒绝猜测归属，也不能把缺失身份当成进程已经消失。"""
    if (record.get("kind") not in {"dsh", "command", "tmux", "worker", "activity"}
            or not isinstance(record.get("individual", False), bool)
            or not isinstance(record.get("members", []), list)):
        raise ValueError("invalid process resource record")
    for identity in [record.get("identity"), *record.get("members", [])]:
        if (not isinstance(identity, dict)
                or any(type(identity.get(key)) is not int or identity[key] <= 0 for key in ("pid", "pgid", "sid"))
                or type(identity.get("uid")) is not int or identity["uid"] < 0
                or not isinstance(identity.get("start_time"), str) or not identity["start_time"].isdigit()
                or not isinstance(identity.get("boot_id"), str) or not identity["boot_id"].strip()):
            raise ValueError("process resource has an incomplete start identity")


def identities() -> list[dict[str, Any]]:
    result = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                value = process_identity(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
            if value is not None:
                result.append(value)
    return result


def register_process(pid: int, kind: str, runtime_file: Path | None = None, *, individual: bool = False) -> dict[str, Any]:
    identity = process_identity(pid)
    record: dict[str, Any] = {"kind": kind, "identity": identity, "members": [], "individual": individual}
    if identity is not None and runtime_file is not None and runtime_file.exists():
        import json
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        record["run_id"] = runtime["run_id"]
        record["record_file"] = str(runtime_file.parent / "resources" / f"{kind}-{pid}-{identity['start_time']}.json")
    snapshot_group(record)
    return record


def snapshot_group(record: dict[str, Any]) -> list[dict[str, Any]]:
    expected = record.get("identity")
    if not expected:
        return []
    processes = identities()
    by_pid = {item["pid"]: item for item in processes}
    leader = by_pid.get(expected["pid"])
    # 新进程占用旧 leader PID 时，禁止通过旧 PGID 扩大清理范围。
    leader_matches = same_process(expected, leader)
    known = [item for item in record.get("members", []) if same_process(item, by_pid.get(item["pid"]))]
    anchor = leader_matches or any(item["sid"] == expected["sid"] for item in known)
    if record.get("individual"):
        members = [leader] if leader_matches else []
    elif anchor and (leader is None or leader_matches):
        members = [item for item in processes if item["sid"] == expected["sid"]]
    else:
        members = [by_pid[item["pid"]] for item in known]
    # 同一 session 内的工具可能建立自己的进程组。自行 setsid 的 daemon 不在此范围；
    # tmux server 与 worker 具有独立登记，不能被 DSH 关闭路径一并结束。
    if members != record.get("members"):
        record["members"] = members
        if path := record.get("record_file"):
            try:
                atomic_write_json(Path(path), {key: value for key, value in record.items() if key != "record_file"})
            except OSError as exc:
                # 证据写入失败不能阻止终止内存中仍可确认身份的进程。
                record["persistence_error"] = str(exc)
    return [item for item in members if item["state"] != "Z"]


def stop_process_group(record: dict[str, Any], sig: int = signal.SIGTERM) -> list[dict[str, Any]]:
    members = snapshot_group(record)
    groups = set()
    for member in members:
        if member["pid"] == os.getpid():
            continue
        if same_process(member, process_identity(member["pid"])):
            try:
                if record.get("individual"):
                    os.kill(member["pid"], sig)
                elif member["pgid"] not in groups and member["pgid"] != os.getpgrp():
                    os.killpg(member["pgid"], sig)
                    groups.add(member["pgid"])
            except ProcessLookupError:
                pass
    return members
