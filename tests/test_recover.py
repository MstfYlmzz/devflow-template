from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from devflow.cli import _cmd_recover
from devflow.gitops import task_worktree
from devflow.lock import lock_path, read_lock
from devflow.taskfile import create, read


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.pid is not None
    return proc.pid


def _write_stale_lock(repo: Path, task_id: int, pid: int) -> None:
    path = lock_path(task_id, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "pid": pid,
                "started_at": "2026-09-08T14:02:00+03:00",
                "host": "test",
                "stage": "implement",
                "agent_pid": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _task(repo: Path, task_id: int = 184) -> Path:
    path = repo / ".devflow" / "tasks" / f"{task_id}.md"
    create(path, task_id, "Order cancel", state="IMPLEMENTING")
    return path


def _bind_repo(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr("devflow.cli.repo_root", lambda: repo)
    monkeypatch.setattr(
        "devflow.cli.task_file",
        lambda task_id: repo / ".devflow" / "tasks" / f"{task_id}.md",
    )


def test_recover_report_only_does_not_change_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _task(git_repo)
    wt = task_worktree(git_repo, 184)
    wt.mkdir(parents=True)
    (wt / "note.txt").write_text("keep\n", encoding="utf-8")
    _write_stale_lock(git_repo, 184, _dead_pid())
    _bind_repo(monkeypatch, git_repo)
    assert _cmd_recover(task_id=184, release_lock=False, abandon=False) == 0
    output = capsys.readouterr().out
    assert "stale lock detected" in output
    assert "not running" in output
    assert "IMPLEMENTING" in output
    assert "devflow recover 184 --release" in output
    assert read(path).frontmatter.state == "IMPLEMENTING"
    assert lock_path(184, git_repo).is_file()
    assert (wt / "note.txt").is_file()


def test_recover_release_sets_blocked_keeps_worktree(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(git_repo)
    wt = task_worktree(git_repo, 184)
    wt.mkdir(parents=True)
    (wt / "note.txt").write_text("keep\n", encoding="utf-8")
    _write_stale_lock(git_repo, 184, _dead_pid())
    _bind_repo(monkeypatch, git_repo)
    assert _cmd_recover(task_id=184, release_lock=True, abandon=False) == 0
    tf = read(path)
    assert tf.frontmatter.state == "BLOCKED"
    assert tf.frontmatter.blocked_from == "IMPLEMENTING"
    assert tf.frontmatter.blocked_reason == "INTERRUPTED"
    assert read_lock(184, git_repo) is None
    assert (wt / "note.txt").is_file()


def test_recover_abandon_removes_worktree_cancels(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(git_repo)
    wt = task_worktree(git_repo, 184)
    wt.mkdir(parents=True)
    (wt / "note.txt").write_text("gone\n", encoding="utf-8")
    _write_stale_lock(git_repo, 184, _dead_pid())
    _bind_repo(monkeypatch, git_repo)
    assert _cmd_recover(task_id=184, release_lock=False, abandon=True) == 0
    tf = read(path)
    assert tf.frontmatter.state == "CANCELLED"
    assert read_lock(184, git_repo) is None
    assert not wt.exists()
