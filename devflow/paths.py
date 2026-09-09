from __future__ import annotations

import subprocess
from pathlib import Path

from devflow import gitops

BASE_REF = "origin/main"


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("not a git repository")
    return Path(result.stdout.strip())


def task_path(repo: Path, task_id: int) -> Path:
    """Return ``.devflow/tasks/<id>.md`` under ``repo`` (no existence check)."""
    return repo / ".devflow" / "tasks" / f"{task_id}.md"


def resolve_task(repo: Path, task_id: int, *, attach: bool = True) -> Path | None:
    """Resolve the canonical task file for an active or legacy task.

    Preference:
    1. Task worktree file, if present
    2. Existing task branch reattached to a worktree (when ``attach``)
    3. Main-checkout legacy/merged file
    4. ``None`` — may be an unmaterialized GitHub Issue
    """
    worktree = gitops.task_worktree(repo, task_id)
    worktree_file = task_path(worktree, task_id)
    if worktree.is_dir() and worktree_file.is_file():
        return worktree_file

    if attach and gitops.task_branch_name(repo, task_id):
        attached = gitops.ensure_task_worktree(repo, task_id, "task", BASE_REF)
        attached_file = task_path(attached, task_id)
        if attached_file.is_file():
            return attached_file

    main_file = task_path(repo, task_id)
    if main_file.is_file():
        return main_file
    return None


def task_file(task_id: int) -> Path:
    """CLI helper: resolve under the current repo root.

    When no file exists anywhere, returns the main-checkout path so callers
    can keep ``not path.is_file()`` error handling.
    """
    root = repo_root()
    found = resolve_task(root, task_id)
    if found is not None:
        return found
    return task_path(root, task_id)


def path_in_worktree(repo: Path, task_id: int, path: Path) -> bool:
    worktree = gitops.task_worktree(repo, task_id)
    if not worktree.is_dir():
        return False
    try:
        path.resolve().relative_to(worktree.resolve())
    except ValueError:
        return False
    return True
