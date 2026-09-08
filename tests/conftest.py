from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_GIT_USER_NAME = "Devflow Test"
_GIT_USER_EMAIL = "test@devflow.local"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Return an empty git repo with a local identity configured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", _GIT_USER_EMAIL)
    git(repo, "config", "user.name", _GIT_USER_NAME)
    return repo
