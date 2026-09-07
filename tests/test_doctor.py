from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from devflow.cli import doctor
from devflow.init import init


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _add_language_stage(root: Path) -> None:
    stage = root / "scripts" / "verify.d" / "10-dummy"
    stage.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stage.chmod(stage.stat().st_mode | 0o111)


def _set_hooks_path(root: Path) -> None:
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


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


def test_doctor_exit_zero_on_complete_repo(tmp_path: Path) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    _add_language_stage(tmp_path)
    _set_hooks_path(tmp_path)
    _mark_policy_reviewed(tmp_path)
    assert doctor(tmp_path) == 0


def test_doctor_exit_one_when_only_preflight(tmp_path: Path) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    assert doctor(tmp_path) == 1


def test_doctor_exit_one_when_directory_removed(tmp_path: Path) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    _add_language_stage(tmp_path)
    shutil.rmtree(tmp_path / ".devflow" / "tasks")
    assert doctor(tmp_path) == 1


def test_doctor_warns_when_hooks_path_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    _add_language_stage(tmp_path)
    _mark_policy_reviewed(tmp_path)
    assert doctor(tmp_path) == 0
    output = capsys.readouterr().out
    assert "warn:" in output
    assert "core.hooksPath not set" in output


def test_doctor_exit_one_when_policy_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    init(tmp_path)
    _add_language_stage(tmp_path)
    (tmp_path / ".ai" / "policy.yml").unlink()
    assert doctor(tmp_path) == 1
    assert "missing file: .ai/policy.yml" in capsys.readouterr().out
