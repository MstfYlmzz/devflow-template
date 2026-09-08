from __future__ import annotations

import inspect
from pathlib import Path

from devflow.gitops import rebase_onto_base
from tests.conftest import git


def test_rebase_onto_base_returns_new_head(git_repo: Path) -> None:
    repo = git_repo
    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "main")
    git(repo, "checkout", "-b", "task")
    (repo / "feature.txt").write_text("task\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "task")
    git(repo, "checkout", "main")
    (repo / "moved.txt").write_text("moved\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "main moved")
    git(repo, "checkout", "task")
    old = git(repo, "rev-parse", "HEAD").stdout.strip()
    new = rebase_onto_base(repo, "main")
    assert new != old
    assert (repo / "feature.txt").is_file()
    assert (repo / "moved.txt").is_file()
    log = git(repo, "log", "--oneline").stdout
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
