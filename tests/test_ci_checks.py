from __future__ import annotations

from pathlib import Path

import pytest

from devflow.ci_checks import changed_files, ci_checks_report
from devflow.cli import _cmd_ci_checks
from devflow.policy import load_policy
from tests.conftest import git

_POLICY = load_policy(
    Path(__file__).resolve().parents[1] / "templates" / "project" / ".ai" / "policy.yml"
)
_ROOT = Path(__file__).resolve().parents[1]


def test_report_control_ok_skips_fast_lane() -> None:
    code, text = ci_checks_report(
        [".ai/policy.yml", "devflow/lock.py", ".gitignore"],
        _POLICY,
    )
    assert code == 0
    assert text == (
        "control files: ok (3 control paths, no unrelated code)\n"
        "fast lane:     skipped (control change)\n"
    )


def test_report_control_fail_skips_fast_lane() -> None:
    code, text = ci_checks_report(
        [".ai/policy.yml", "src/orders/service.py"],
        _POLICY,
    )
    assert code == 1
    assert text == (
        "control files: FAIL — .ai/policy.yml alongside src/orders/service.py\n"
        "fast lane:     skipped\n"
    )


def test_report_src_fails_fast_lane() -> None:
    code, text = ci_checks_report(["src/orders/service.py"], _POLICY)
    assert code == 1
    assert text == (
        "control files: ok (0 control paths, no unrelated code)\n"
        "fast lane:     FAIL — src/orders/service.py: unmatched path\n"
    )


def test_report_css_is_fast_lane_ok() -> None:
    code, text = ci_checks_report(["styles/main.css"], _POLICY)
    assert code == 0
    assert text == (
        "control files: ok (0 control paths, no unrelated code)\n"
        "fast lane:     ok (all paths fast-lane eligible)\n"
    )


def test_report_empty_diff_skips_fast_lane() -> None:
    code, text = ci_checks_report([], _POLICY)
    assert code == 0
    assert text == (
        "control files: ok (0 control paths, no unrelated code)\n"
        "fast lane:     skipped (no changes)\n"
    )


def test_report_task_file_skips_fast_lane() -> None:
    code, text = ci_checks_report(
        [".devflow/tasks/184.md", "src/orders/service.py"],
        _POLICY,
    )
    assert code == 0
    assert text == (
        "control files: ok (0 control paths, no unrelated code)\n"
        "fast lane:     skipped (task file)\n"
    )


def test_changed_files_lists_triple_dot_diff(git_repo: Path) -> None:
    git("commit", "--allow-empty", "-m", "base", cwd=git_repo)
    git("checkout", "-b", "feat", cwd=git_repo)
    (git_repo / "README.md").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=git_repo)
    git("commit", "-m", "readme", cwd=git_repo)
    assert changed_files(git_repo, "main") == ["README.md"]


def test_cmd_ci_checks_mixed_control_and_src(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git("commit", "--allow-empty", "-m", "base", cwd=git_repo)
    git("checkout", "-b", "feat", cwd=git_repo)
    (git_repo / ".ai").mkdir()
    (git_repo / ".ai" / "policy.yml").write_text("x\n", encoding="utf-8")
    (git_repo / "src" / "orders").mkdir(parents=True)
    (git_repo / "src" / "orders" / "service.py").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=git_repo)
    git("commit", "-m", "mixed", cwd=git_repo)
    monkeypatch.setattr("devflow.cli.repo_root", lambda: git_repo)
    monkeypatch.chdir(git_repo)
    assert _cmd_ci_checks(base="main") == 1
    assert capsys.readouterr().out == (
        "control files: FAIL — .ai/policy.yml alongside src/orders/service.py\n"
        "fast lane:     skipped\n"
    )


def test_cmd_ci_checks_empty_diff(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git("commit", "--allow-empty", "-m", "base", cwd=git_repo)
    monkeypatch.setattr("devflow.cli.repo_root", lambda: git_repo)
    monkeypatch.chdir(git_repo)
    assert _cmd_ci_checks(base="main") == 0
    assert capsys.readouterr().out == (
        "control files: ok (0 control paths, no unrelated code)\n"
        "fast lane:     skipped (no changes)\n"
    )


def test_cmd_ci_checks_missing_base(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git("commit", "--allow-empty", "-m", "base", cwd=git_repo)
    monkeypatch.chdir(git_repo)
    assert _cmd_ci_checks(base="origin/main") == 1
    assert capsys.readouterr().err


def test_workflows_call_ci_checks() -> None:
    for name in ("control-guard.yml", "fast-lane.yml"):
        text = (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "devflow ci-checks" in text
        assert "python <<'" not in text
        assert "check_control_changes" not in text
        assert "fast_lane_eligible" not in text


def test_typecheck_uses_linux_platform() -> None:
    text = (_ROOT / "scripts" / "verify.d" / "30-typecheck").read_text(encoding="utf-8")
    assert "mypy --platform linux" in text


def test_pre_push_runs_ci_checks() -> None:
    text = (_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert "devflow ci-checks" in text
    assert "DEVFLOW_SKIP_HOOKS" in text
    assert "skipping ci-checks" in text
