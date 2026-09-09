"""Authority separation: agents cannot merge, and branches cannot rewrite review rules.

sanitized_env() strips tokens from the agent subprocess environment. That is
not sufficient on its own: the gh CLI can still authenticate from a saved
session on disk. This function is one layer, not the whole defence.

The binding rule, enforced by command construction in this project, is that
the agent never runs push, PR, or merge commands. Those operations are
executed by devflow, not by the agent.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devflow.policy import load_policy, validate_policy

STRIP_ENV_KEYS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_ASKPASS",
    "SSH_AUTH_SOCK",
    "GH_CONFIG_DIR",
)

FORBIDDEN_AGENT_COMMANDS: tuple[str, ...] = (
    "gh pr merge",
    "gh pr create",
    "git push",
    "gh auth",
)

CONTROL_PATHS: list[str] = [
    ".ai/**",
    "scripts/verify",
    "scripts/verify.d/**",
    # Isolated worktree venv setup; skipping it silently tests the main checkout.
    "scripts/setup-worktree",
    ".github/workflows/**",
    ".githooks/**",
    # In this repo, devflow's own source is part of the control mechanism
    # (check_control_changes lives here). Consumer projects keep app code
    # under src/** and depend on devflow as a package.
    "devflow/**",
    # Everything under templates/project/ defines the consumer project's
    # control mechanism: policy, role prompts, document skeleton, and the
    # verify contract. None of it is application code.
    # In this repo, "unrelated application code" is practically only src/**
    # and similar paths; no such directory exists here. The rule does its
    # real work in consumer projects.
    "templates/project/**",
    # .gitignore is part of the control mechanism: it keeps lock files,
    # worktrees, and local config unversioned. Removing a line can leak secrets.
    ".gitignore",
    # Line endings for bash scripts; CRLF breaks `set -euo pipefail` / `exit 1`.
    ".gitattributes",
]

_ALLOWED_WITH_CONTROL: tuple[str, ...] = (
    "tests/**",
    "docs/**",
    ".devflow/tasks/**",
    "README*",
)


@dataclass
class ControlChangeResult:
    touches_control: bool
    control_files: list[str]
    unrelated_code_files: list[str]
    ok: bool
    message: str


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment for an agent subprocess with credentials removed.

    Removing these variables is not a complete sandbox. gh can still use a
    saved session. Agents must never be given push, PR, or merge commands;
    devflow runs those itself.
    """
    source = os.environ if base is None else base
    env = {str(key): str(value) for key, value in source.items()}
    for key in STRIP_ENV_KEYS:
        env.pop(key, None)
    return env


def check_agent_output_for_violations(output: str) -> list[str]:
    """Report traces of forbidden commands in agent output. Detection only."""
    findings: list[str] = []
    for command in FORBIDDEN_AGENT_COMMANDS:
        if command in output:
            findings.append(f"agent output mentions forbidden command: {command}")
    return findings


def load_policy_from_base(repo: Path, base_ref: str = "origin/main") -> dict[str, Any]:
    """Load `.ai/policy.yml` from base_ref, not the working tree.

    A branch must not be able to weaken the rules that review it. If the
    base ref is missing, this fails instead of falling back to the worktree.
    An invalid base policy fails instead of producing a silent default.
    """
    spec = f"{base_ref}:.ai/policy.yml"
    result = subprocess.run(
        ["git", "show", spec],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git show failed"
        raise RuntimeError(
            f"could not read .ai/policy.yml from {base_ref} "
            f"(no working-tree fallback): {detail}"
        )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        delete=False,
        encoding="utf-8",
    )
    tmp_path = Path(handle.name)
    try:
        handle.write(result.stdout)
        handle.close()
        policy = load_policy(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    errors = validate_policy(policy)
    if errors:
        listed = "\n".join(f"  {line}" for item in errors for line in item.splitlines())
        raise RuntimeError(
            f"policy at {base_ref} is invalid:\n{listed}\n"
            "fix and merge before running devflow start"
        )
    return policy


def check_control_changes(changed: list[str]) -> ControlChangeResult:
    if not changed:
        return ControlChangeResult(
            touches_control=False,
            control_files=[],
            unrelated_code_files=[],
            ok=True,
            message="",
        )
    control_files: list[str] = []
    unrelated_code_files: list[str] = []
    for raw in changed:
        path = _posix(raw)
        if _matches_any(path, CONTROL_PATHS):
            control_files.append(path)
        elif _matches_any(path, _ALLOWED_WITH_CONTROL):
            continue
        else:
            unrelated_code_files.append(path)

    touches_control = bool(control_files)
    ok = not (touches_control and unrelated_code_files)
    message = ""
    if not ok:
        message = (
            f"control files changed ({', '.join(control_files)}) "
            f"alongside unrelated code ({', '.join(unrelated_code_files)}) "
            "— split into separate PRs"
        )
    return ControlChangeResult(
        touches_control=touches_control,
        control_files=control_files,
        unrelated_code_files=unrelated_code_files,
        ok=ok,
        message=message,
    )


def is_control_change(changed: list[str]) -> bool:
    """True when every path is a control file or an allowed companion."""
    if not changed:
        return False
    for raw in changed:
        path = _posix(raw)
        if _matches_any(path, CONTROL_PATHS):
            continue
        if _matches_any(path, _ALLOWED_WITH_CONTROL):
            continue
        return False
    return True


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _matches(path: str, pattern: str) -> bool:
    path = _posix(path)
    pattern = _posix(pattern)
    if pattern.endswith("/**"):
        root = pattern[:-3]
        return path == root or path.startswith(f"{root}/")
    return fnmatch.fnmatch(path, pattern)


def _matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)
