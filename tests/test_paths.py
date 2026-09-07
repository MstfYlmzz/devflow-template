from __future__ import annotations

from pathlib import Path

import pytest

from devflow.paths import repo_root, task_file


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


def test_task_file() -> None:
    path = task_file(42)
    assert path == repo_root() / ".devflow" / "tasks" / "42.md"
    assert not path.exists()
