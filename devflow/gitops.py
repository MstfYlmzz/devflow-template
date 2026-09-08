"""Git operations used by devflow. Agents never invoke these.

rebase_onto_base() rewrites the worktree onto base_ref and returns the new
HEAD SHA. It does not push. Force-with-lease belongs only at push time.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def git_output(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def rebase_onto_base(worktree: Path, base_ref: str) -> str:
    """Rebase the worktree onto base_ref and return the new HEAD SHA.

    Does not push. The runner must not call this until step 12.
    """
    rebased = subprocess.run(
        ["git", "rebase", base_ref],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if rebased.returncode != 0:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        detail = rebased.stderr.strip() or rebased.stdout.strip() or "rebase failed"
        raise RuntimeError(f"rebase onto {base_ref} failed: {detail}")
    return git_output(worktree, "rev-parse", "HEAD")


@dataclass
class ResumeContext:
    worktree_exists: bool
    branch_exists: bool
    uncommitted_files: list[str]
    last_commit_sha: str | None


def task_worktree(repo: Path, task_id: int) -> Path:
    return repo / ".devflow" / "worktrees" / f"task-{task_id}"


def inspect_resume(task_id: int, repo: Path) -> ResumeContext:
    worktree = task_worktree(repo, task_id)
    worktree_exists = worktree.is_dir()
    matching = _task_branches(repo, task_id)
    uncommitted: list[str] = []
    last_commit_sha: str | None = None
    branch_exists = bool(matching)
    if worktree_exists and _has_git_dir(worktree):
        try:
            last_commit_sha = git_output(worktree, "rev-parse", "HEAD")
        except RuntimeError:
            last_commit_sha = None
        try:
            porcelain = git_output(worktree, "status", "--porcelain")
        except RuntimeError:
            porcelain = ""
        uncommitted = _porcelain_paths(porcelain)
        try:
            current = git_output(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        except RuntimeError:
            current = ""
        if current and current != "HEAD":
            branch_exists = True
    return ResumeContext(
        worktree_exists=worktree_exists,
        branch_exists=branch_exists,
        uncommitted_files=uncommitted,
        last_commit_sha=last_commit_sha,
    )


def task_branch_name(repo: Path, task_id: int) -> str | None:
    worktree = task_worktree(repo, task_id)
    if worktree.is_dir() and _has_git_dir(worktree):
        try:
            current = git_output(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        except RuntimeError:
            current = ""
        if current and current != "HEAD":
            return current
    matching = _task_branches(repo, task_id)
    return matching[0] if matching else None


def _has_git_dir(path: Path) -> bool:
    return (path / ".git").exists()


def remove_task_worktree(repo: Path, task_id: int) -> None:
    worktree = task_worktree(repo, task_id)
    if not worktree.exists():
        return
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if removed.returncode == 0:
        return
    if worktree.exists():
        shutil.rmtree(worktree)


def _task_branches(repo: Path, task_id: int) -> list[str]:
    try:
        refs = git_output(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
        )
    except RuntimeError:
        return []
    prefix = f"task/{task_id}"
    names: list[str] = []
    for name in refs.splitlines():
        if name == prefix or name.startswith(f"{prefix}-"):
            names.append(name)
    return names


def _porcelain_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths
