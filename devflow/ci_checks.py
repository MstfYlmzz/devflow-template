"""PR checks shared by CI workflows and local `devflow ci-checks`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devflow.authority import (
    ControlChangeResult,
    check_control_changes,
    is_control_change,
)
from devflow.gitops import git_output
from devflow.policy import fast_lane_eligible

_CONTROL_LABEL = "control files: "
_FAST_LABEL = "fast lane:     "
_TASK_PREFIX = ".devflow/tasks/"


def changed_files(repo: Path, base: str) -> list[str]:
    output = git_output(repo, "diff", "--name-only", f"{base}...HEAD")
    return [line.replace("\\", "/") for line in output.splitlines() if line]


def ci_checks_report(changed: list[str], policy: dict[str, Any]) -> tuple[int, str]:
    control = check_control_changes(changed)
    fast_line, fast_ok = _fast_lane_line(changed, control, policy)
    text = f"{_control_line(control)}\n{fast_line}\n"
    return (0 if control.ok and fast_ok else 1), text


def needs_fast_lane(changed: list[str]) -> bool:
    control = check_control_changes(changed)
    return _fast_lane_should_run(changed, control)


def _fast_lane_should_run(changed: list[str], control: ControlChangeResult) -> bool:
    if not control.ok:
        return False
    if any(path.startswith(_TASK_PREFIX) for path in changed):
        return False
    return not is_control_change(changed)


def _control_line(control: ControlChangeResult) -> str:
    if not control.ok:
        control_joined = ", ".join(control.control_files)
        unrelated_joined = ", ".join(control.unrelated_code_files)
        return f"{_CONTROL_LABEL}FAIL — {control_joined} alongside {unrelated_joined}"
    n = len(control.control_files)
    noun = "path" if n == 1 else "paths"
    return f"{_CONTROL_LABEL}ok ({n} control {noun}, no unrelated code)"


def _fast_lane_line(
    changed: list[str],
    control: ControlChangeResult,
    policy: dict[str, Any],
) -> tuple[str, bool]:
    if not _fast_lane_should_run(changed, control):
        if not control.ok:
            return f"{_FAST_LABEL}skipped", True
        if any(path.startswith(_TASK_PREFIX) for path in changed):
            return f"{_FAST_LABEL}skipped (task file)", True
        return f"{_FAST_LABEL}skipped (control change)", True
    eligible, reason = fast_lane_eligible(changed, policy)
    if eligible:
        return f"{_FAST_LABEL}ok ({reason})", True
    return f"{_FAST_LABEL}FAIL — {reason}", False
