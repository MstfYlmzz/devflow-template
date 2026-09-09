from __future__ import annotations

from pathlib import Path

import pytest

from devflow.paths import repo_root, resolve_task, task_file, task_path


def test_repo_root() -> None:
    root = repo_root()
    assert isinstance(root, Path)
    assert root.is_dir()
    assert (root / ".git").exists()


def test_repo_root_raises_outside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError):
        repo_root()


def test_task_file_missing_falls_back_to_main_path() -> None:
    path = task_file(42)
    assert path == repo_root() / ".devflow" / "tasks" / "42.md"
    assert not path.exists()


def test_resolve_task_prefers_worktree(git_repo: Path) -> None:
    from tests.conftest import git

    (git_repo / "README").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=git_repo)
    git("commit", "-m", "base", cwd=git_repo)
    git(
        "update-ref",
        "refs/remotes/origin/main",
        git("rev-parse", "HEAD", cwd=git_repo).stdout.strip(),
        cwd=git_repo,
    )
    main = task_path(git_repo, 7)
    main.parent.mkdir(parents=True)
    main.write_text("---\nid: 7\ntitle: main\nstate: BACKLOG\n---\n", encoding="utf-8")
    from devflow.gitops import ensure_task_worktree
    from devflow.taskfile import create

    wt = ensure_task_worktree(git_repo, 7, "wt", "origin/main")
    create(task_path(wt, 7), 7, "wt", state="PLAN_APPROVAL")
    assert resolve_task(git_repo, 7) == task_path(wt, 7)


def test_runner_uses_shared_resolve_task() -> None:
    source = Path(__file__).resolve().parents[1] / "devflow" / "runner.py"
    text = source.read_text(encoding="utf-8")
    assert "resolve_task(" in text
    assert "def _task_path" not in text
    assert 'repo / ".devflow" / "tasks"' not in text
