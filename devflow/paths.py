from __future__ import annotations

import subprocess
from pathlib import Path


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


def task_file(task_id: int) -> str:
    return repo_root() / ".devflow" / "tasks" / f"{task_id}.md"
