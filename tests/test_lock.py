from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devflow.lock import (
    LockHeld,
    StaleLock,
    acquire,
    is_process_alive,
    lock_path,
    read_lock,
    release,
)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.pid is not None
    return proc.pid


def test_acquire_creates_lock_file(tmp_path: Path) -> None:
    lock = acquire(184, "implement", tmp_path)
    path = lock_path(184, tmp_path)
    assert path.is_file()
    assert lock.task_id == 184
    assert lock.pid == os.getpid()
    assert lock.stage == "implement"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["task_id"] == 184
    assert read_lock(184, tmp_path) == lock


def test_second_acquire_while_alive_raises_lock_held(tmp_path: Path) -> None:
    acquire(184, "implement", tmp_path)
    with pytest.raises(LockHeld, match="task 184 is already running"):
        acquire(184, "review", tmp_path)


def test_second_acquire_when_dead_raises_stale_lock(tmp_path: Path) -> None:
    dead = _dead_pid()
    path = lock_path(184, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": 184,
                "pid": dead,
                "started_at": "2026-09-08T14:02:00+03:00",
                "host": "test",
                "stage": "implement",
                "agent_pid": None,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StaleLock, match="stale lock from a crashed run"):
        acquire(184, "implement", tmp_path)
    assert path.is_file()


def test_release_removes_lock_file(tmp_path: Path) -> None:
    acquire(7, "triage", tmp_path)
    release(7, tmp_path)
    assert read_lock(7, tmp_path) is None
    assert not lock_path(7, tmp_path).is_file()


def test_release_missing_lock_is_idempotent(tmp_path: Path) -> None:
    release(99, tmp_path)


def test_is_process_alive_own_pid() -> None:
    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_impossible_pid() -> None:
    assert is_process_alive(0) is False
    assert is_process_alive(-1) is False
    assert is_process_alive(_dead_pid()) is False
