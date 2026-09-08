"""Run locks for interrupted work. Locks are never auto-resumed.

A lock records that a run is in progress. If the process dies, the lock
stays until a human runs `devflow recover`. Stale locks are not deleted
by acquire().
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from devflow.taskfile import atomic_write

Stage = Literal["start", "triage", "implement", "review"]
_STAGES = {"start", "triage", "implement", "review"}

LOCKS_GITIGNORE_LINE = ".devflow/locks/"


@dataclass
class RunLock:
    task_id: int
    pid: int
    started_at: str
    host: str
    stage: str
    agent_pid: int | None


class LockHeld(Exception):
    def __init__(self, lock: RunLock) -> None:
        self.lock = lock
        super().__init__(
            f"task {lock.task_id} is already running "
            f"(pid {lock.pid}, started {_hhmm(lock.started_at)})"
        )


class StaleLock(Exception):
    def __init__(self, lock: RunLock) -> None:
        self.lock = lock
        super().__init__(
            f"task {lock.task_id} has a stale lock from a crashed run (pid {lock.pid})"
        )


def lock_path(task_id: int, repo: Path) -> Path:
    return repo / ".devflow" / "locks" / f"{task_id}.json"


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock(task_id: int, repo: Path) -> RunLock | None:
    path = lock_path(task_id, repo)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid lock file: {path}")
    return RunLock(
        task_id=int(raw["task_id"]),
        pid=int(raw["pid"]),
        started_at=str(raw["started_at"]),
        host=str(raw["host"]),
        stage=str(raw["stage"]),
        agent_pid=None if raw.get("agent_pid") is None else int(raw["agent_pid"]),
    )


def acquire(task_id: int, stage: Stage, repo: Path) -> RunLock:
    if stage not in _STAGES:
        raise ValueError(f"invalid lock stage: {stage!r}")
    existing = read_lock(task_id, repo)
    if existing is not None:
        if is_process_alive(existing.pid):
            raise LockHeld(existing)
        raise StaleLock(existing)
    lock = RunLock(
        task_id=task_id,
        pid=os.getpid(),
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        host=socket.gethostname(),
        stage=stage,
        agent_pid=None,
    )
    path = lock_path(task_id, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(asdict(lock), indent=2) + "\n")
    return lock


def release(task_id: int, repo: Path) -> None:
    path = lock_path(task_id, repo)
    path.unlink(missing_ok=True)


def set_agent_pid(task_id: int, repo: Path, agent_pid: int | None) -> None:
    existing = read_lock(task_id, repo)
    if existing is None:
        return
    updated = RunLock(
        task_id=existing.task_id,
        pid=existing.pid,
        started_at=existing.started_at,
        host=existing.host,
        stage=existing.stage,
        agent_pid=agent_pid,
    )
    atomic_write(lock_path(task_id, repo), json.dumps(asdict(updated), indent=2) + "\n")


def ignores_lock_dir(gitignore_text: str) -> bool:
    for raw in gitignore_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in {LOCKS_GITIGNORE_LINE, ".devflow/locks"}:
            return True
    return False


def _hhmm(started_at: str) -> str:
    try:
        return datetime.fromisoformat(started_at).strftime("%H:%M")
    except ValueError:
        return started_at


def _windows_pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    # Linux mypy has no ctypes.windll; Windows mypy does, so unused-ignore too.
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined, unused-ignore]
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if ok == 0:
            return False
        return int(code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)
