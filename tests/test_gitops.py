from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

from devflow.gitops import rebase_onto_base


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "devflow",
        "GIT_AUTHOR_EMAIL": "devflow@example.com",
        "GIT_COMMITTER_NAME": "devflow",
        "GIT_COMMITTER_EMAIL": "devflow@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=devflow",
            "-c",
            "user.email=devflow@example.com",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def test_rebase_onto_base_returns_new_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main")
    _git(repo, "checkout", "-b", "task")
    (repo / "feature.txt").write_text("task\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "task")
    _git(repo, "checkout", "main")
    (repo / "moved.txt").write_text("moved\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main moved")
    _git(repo, "checkout", "task")
    old = _git(repo, "rev-parse", "HEAD").stdout.strip()
    new = rebase_onto_base(repo, "main")
    assert new != old
    assert (repo / "feature.txt").is_file()
    assert (repo / "moved.txt").is_file()
    log = _git(repo, "log", "--oneline").stdout
    assert "main moved" in log


def test_rebase_onto_base_does_not_push() -> None:
    source = inspect.getsource(rebase_onto_base)
    assert '"push"' not in source
    assert "'push'" not in source
    assert "force-with-lease" not in source
    assert "--force" not in source


def test_rebase_onto_base_is_not_called_elsewhere() -> None:
    root = Path(__file__).resolve().parents[1] / "devflow"
    for path in root.rglob("*.py"):
        if path.name == "gitops.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "rebase_onto_base" not in text
