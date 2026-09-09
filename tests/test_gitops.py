from __future__ import annotations

import inspect
from pathlib import Path

from devflow.gitops import inspect_resume, rebase_onto_base
from tests.conftest import git


def test_rebase_onto_base_returns_new_head(git_repo: Path) -> None:
    repo = git_repo
    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main", cwd=repo)
    git("checkout", "-b", "task", cwd=repo)
    (repo / "feature.txt").write_text("task\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "task", cwd=repo)
    git("checkout", "main", cwd=repo)
    (repo / "moved.txt").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main moved", cwd=repo)
    git("checkout", "task", cwd=repo)
    old = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    new = rebase_onto_base(repo, "main")
    assert new != old
    assert (repo / "feature.txt").is_file()
    assert (repo / "moved.txt").is_file()
    log = git("log", "--oneline", cwd=repo).stdout
    assert "main moved" in log


def test_rebase_onto_base_does_not_push() -> None:
    source = inspect.getsource(rebase_onto_base)
    assert '"push"' not in source
    assert "'push'" not in source
    assert "force-with-lease" not in source
    assert "--force" not in source


def test_rebase_onto_base_is_not_called_elsewhere() -> None:
    root = Path(__file__).resolve().parents[1] / "devflow"
    allowed = {"gitops.py", "runner.py"}
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        assert "rebase_onto_base" not in text


def test_inspect_resume_missing_worktree(git_repo: Path) -> None:
    ctx = inspect_resume(184, git_repo)
    assert ctx.worktree_exists is False
    assert ctx.branch_exists is False
    assert ctx.uncommitted_files == []
    assert ctx.last_commit_sha is None


def test_inspect_resume_uncommitted_files(git_repo: Path) -> None:
    (git_repo / "README").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=git_repo)
    git("commit", "-m", "base", cwd=git_repo)
    worktree = git_repo / ".devflow" / "worktrees" / "task-184"
    git(
        "worktree",
        "add",
        "-b",
        "task/184-order-cancel",
        str(worktree),
        cwd=git_repo,
    )
    (worktree / "dirty.txt").write_text("n\n", encoding="utf-8")
    ctx = inspect_resume(184, git_repo)
    assert ctx.worktree_exists is True
    assert ctx.branch_exists is True
    assert "dirty.txt" in ctx.uncommitted_files
    assert ctx.last_commit_sha is not None


def test_inspect_resume_plain_directory_ignores_parent_repo(git_repo: Path) -> None:
    (git_repo / "README").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=git_repo)
    git("commit", "-m", "base", cwd=git_repo)
    (git_repo / "parent-only.py").write_text("x\n", encoding="utf-8")
    worktree = git_repo / ".devflow" / "worktrees" / "task-184"
    worktree.mkdir(parents=True)
    (worktree / "note.txt").write_text("n\n", encoding="utf-8")
    ctx = inspect_resume(184, git_repo)
    assert ctx.worktree_exists is True
    assert ctx.uncommitted_files == []
    assert ctx.last_commit_sha is None
