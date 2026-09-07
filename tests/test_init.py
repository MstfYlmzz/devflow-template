from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from devflow.init import init


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_init_creates_expected_files(tmp_path: Path) -> None:
    _git_init(tmp_path)
    report = init(tmp_path)

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
        dest = tmp_path / rel
        assert dest.is_file()
        assert dest in report.created

    assert (tmp_path / "docs/architecture").is_dir()
    assert (tmp_path / "docs/requirements").is_dir()
    assert (tmp_path / ".ai").is_dir()
    assert (tmp_path / ".devflow/tasks").is_dir()
    assert not (tmp_path / "docs/architecture/.gitkeep").exists()


def test_init_requires_git_repo(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        init(tmp_path)


def test_init_skips_existing_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    existing = tmp_path / "AGENTS.md"
    existing.write_text("keep me\n", encoding="utf-8")
    report = init(tmp_path)
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    assert existing in report.skipped
    assert existing not in report.created


def test_init_does_not_copy_language_stages(tmp_path: Path) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    names = {path.name for path in (tmp_path / "scripts/verify.d").iterdir()}
    assert "00-preflight" in names
    assert "10-format" not in names
    assert "20-lint" not in names
    assert "30-typecheck" not in names
    assert "40-test" not in names


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_copied_verify_is_executable(tmp_path: Path) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    mode = (tmp_path / "scripts/verify").stat().st_mode
    assert mode & stat.S_IXUSR
