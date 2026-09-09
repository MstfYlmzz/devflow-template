from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from devflow.cli import doctor
from devflow.init import init
from tests.conftest import git


def _add_language_stage(root: Path) -> None:
    stage = root / "scripts" / "verify.d" / "10-dummy"
    stage.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stage.chmod(stage.stat().st_mode | 0o111)


def _set_hooks_path(root: Path) -> None:
    git("config", "core.hooksPath", ".githooks", cwd=root)


def _mark_policy_reviewed(root: Path) -> None:
    path = root / ".ai" / "policy.yml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "reviewed_for_this_project: false",
            "reviewed_for_this_project: true",
            1,
        ),
        encoding="utf-8",
    )


def test_doctor_exit_zero_on_complete_repo(git_repo: Path) -> None:
    init(git_repo)
    _add_language_stage(git_repo)
    _set_hooks_path(git_repo)
    _mark_policy_reviewed(git_repo)
    assert doctor(git_repo) == 0


def test_doctor_exit_one_when_only_preflight(git_repo: Path) -> None:
    init(git_repo)
    assert doctor(git_repo) == 1


def test_doctor_exit_one_when_directory_removed(git_repo: Path) -> None:
    init(git_repo)
    _add_language_stage(git_repo)
    shutil.rmtree(git_repo / ".devflow" / "tasks")
    assert doctor(git_repo) == 1


def test_doctor_warns_when_hooks_path_missing(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init(git_repo)
    _add_language_stage(git_repo)
    _mark_policy_reviewed(git_repo)
    assert doctor(git_repo) == 0
    output = capsys.readouterr().out
    assert "warn:" in output
    assert "core.hooksPath not set" in output


def test_doctor_exit_one_when_policy_missing(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init(git_repo)
    _add_language_stage(git_repo)
    (git_repo / ".ai" / "policy.yml").unlink()
    assert doctor(git_repo) == 1
    assert "missing file: .ai/policy.yml" in capsys.readouterr().out


def test_doctor_warns_when_locks_not_gitignored(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init(git_repo)
    _add_language_stage(git_repo)
    _set_hooks_path(git_repo)
    _mark_policy_reviewed(git_repo)
    (git_repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    assert doctor(git_repo) == 0
    output = capsys.readouterr().out
    assert "warn:" in output
    assert ".devflow/locks/" in output
