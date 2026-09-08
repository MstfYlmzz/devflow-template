from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from devflow.init import init


def test_init_creates_expected_files(git_repo: Path) -> None:
    report = init(git_repo)

    expected_files = [
        "AGENTS.md",
        "ARCHITECTURE.md",
        "docs/adr/README.md",
        "docs/adr/ADR-000-template.md",
        "docs/testing/strategy.md",
        ".ai/policy.yml",
        ".ai/roles/triage.md",
        ".ai/roles/implementer.md",
        ".ai/roles/reviewer.md",
        ".ai/review-schema.md",
        "scripts/verify",
        "scripts/verify.d/00-preflight",
        ".githooks/pre-commit",
        ".githooks/pre-push",
        "scripts/setup-hooks",
    ]
    for rel in expected_files:
        dest = git_repo / rel
        assert dest.is_file()
        assert dest in report.created

    assert (git_repo / "docs/architecture").is_dir()
    assert (git_repo / "docs/requirements").is_dir()
    assert (git_repo / ".ai").is_dir()
    assert (git_repo / ".devflow/tasks").is_dir()
    assert not (git_repo / "docs/architecture/.gitkeep").exists()
    gitignore = (git_repo / ".gitignore").read_text(encoding="utf-8")
    assert ".devflow/locks/" in gitignore


def test_init_requires_git_repo(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        init(tmp_path)


def test_init_skips_existing_file(git_repo: Path) -> None:
    existing = git_repo / "AGENTS.md"
    existing.write_text("keep me\n", encoding="utf-8")
    report = init(git_repo)
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    assert existing in report.skipped
    assert existing not in report.created


def test_init_appends_locks_gitignore(git_repo: Path) -> None:
    existing = git_repo / ".gitignore"
    existing.write_text("*.pyc\n", encoding="utf-8")
    init(git_repo)
    text = existing.read_text(encoding="utf-8")
    assert "*.pyc" in text
    assert ".devflow/locks/" in text


def test_init_does_not_copy_language_stages(git_repo: Path) -> None:
    init(git_repo)
    names = {path.name for path in (git_repo / "scripts/verify.d").iterdir()}
    assert "00-preflight" in names
    assert "10-format" not in names
    assert "20-lint" not in names
    assert "30-typecheck" not in names
    assert "40-test" not in names


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_copied_verify_is_executable(git_repo: Path) -> None:
    init(git_repo)
    mode = (git_repo / "scripts/verify").stat().st_mode
    assert mode & stat.S_IXUSR
