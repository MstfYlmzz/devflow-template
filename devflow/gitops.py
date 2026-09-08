"""Git operations used by devflow. Agents never invoke these.

rebase_onto_base() rewrites the worktree onto base_ref and returns the new
HEAD SHA. It does not push. Force-with-lease belongs only at push time.
"""

from __future__ import annotations

import subprocess
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
