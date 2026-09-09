"""Git operations used by devflow. Agents never invoke these.

rebase_onto_base() rewrites the worktree onto base_ref and returns the new
HEAD SHA. It does not push. Force-with-lease belongs only at push time.
The runner calls this after the implementer commit and before review.
"""

from __future__ import annotations

import re
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


def review_worktree_path(repo: Path, task_id: int, round_no: int) -> Path:
    return repo / ".devflow" / "worktrees" / f"review-{task_id}-r{round_no}"


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return slug or "task"


def commit_all(worktree: Path, message: str) -> str:
    git_output(worktree, "add", "-A")
    porcelain = git_output(worktree, "status", "--porcelain")
    if porcelain.strip():
        git_output(worktree, "commit", "-m", message)
    return git_output(worktree, "rev-parse", "HEAD")


def commit_paths(worktree: Path, message: str, *paths: str) -> str:
    """Stage and commit only the given paths (relative to worktree)."""
    if not paths:
        return git_output(worktree, "rev-parse", "HEAD")
    git_output(worktree, "add", "--", *paths)
    porcelain = git_output(worktree, "status", "--porcelain", "--", *paths)
    if porcelain.strip():
        git_output(worktree, "commit", "-m", message)
    return git_output(worktree, "rev-parse", "HEAD")


def ensure_task_worktree(repo: Path, task_id: int, title: str, base_ref: str) -> Path:
    path = task_worktree(repo, task_id)
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = task_branch_name(repo, task_id)
    if existing:
        git_output(repo, "worktree", "add", str(path), existing)
        return path
    branch = f"task/{task_id}-{slugify(title)}"
    git_output(repo, "worktree", "add", "-b", branch, str(path), base_ref)
    return path


def add_review_worktree(
    repo: Path, task_id: int, round_no: int, start_point: str
) -> Path:
    path = review_worktree_path(repo, task_id, round_no)
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"review/{task_id}-r{round_no}"
    git_output(repo, "worktree", "add", "-b", branch, str(path), start_point)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    if not path.exists():
        return
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if removed.returncode == 0:
        return
    if path.exists():
        shutil.rmtree(path)


def delete_local_branch(repo: Path, name: str) -> None:
    subprocess.run(
        ["git", "branch", "-D", name],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


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
    remove_worktree(repo, task_worktree(repo, task_id))


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
