from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_GIT_USER_NAME = "Devflow Test"
_GIT_USER_EMAIL = "test@devflow.local"


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git with a required working directory.

    cwd is keyword-only so a missing checkout cannot silently use the
    process cwd (and climb into the host repository).
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _host_git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _local_branches(root: Path) -> set[str]:
    text = _host_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return {line for line in text.splitlines() if line}


def _local_config(root: Path) -> set[str]:
    text = _host_git(root, "config", "--local", "--list")
    return {line for line in text.splitlines() if line}


@pytest.fixture(autouse=True)
def _isolate_git_from_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's git lookups inside pytest's temp tree."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent.resolve()))
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    null_config = tmp_path / ".gitconfig-null"
    null_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(null_config.resolve()))


@pytest.fixture(scope="session", autouse=True)
def _guard_host_repository(pytestconfig: pytest.Config) -> object:
    """Fail the session if tests mutate the host repository."""
    root = Path(pytestconfig.rootpath)
    before_branches = _local_branches(root)
    before_config = _local_config(root)
    yield
    after_branches = _local_branches(root)
    after_config = _local_config(root)
    added = sorted(after_branches - before_branches)
    removed = sorted(before_branches - after_branches)
    config_added = sorted(after_config - before_config)
    config_removed = sorted(before_config - after_config)
    if not (added or removed or config_added or config_removed):
        return
    parts: list[str] = []
    if added:
        parts.append(f"branches added: {added}")
    if removed:
        parts.append(f"branches removed: {removed}")
    if config_added:
        parts.append(f"config added: {config_added}")
    if config_removed:
        parts.append(f"config removed: {config_removed}")
    raise AssertionError("tests modified the host repository: " + "; ".join(parts))


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Return an empty git repo with a local identity configured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", _GIT_USER_EMAIL, cwd=repo)
    git("config", "user.name", _GIT_USER_NAME, cwd=repo)
    return repo
