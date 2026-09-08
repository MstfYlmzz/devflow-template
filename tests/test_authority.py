from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from devflow.authority import (
    CONTROL_PATHS,
    STRIP_ENV_KEYS,
    check_agent_output_for_violations,
    check_control_changes,
    load_policy_from_base,
    sanitized_env,
)

_MIN_POLICY = {
    "floor": {},
    "signal_floor": {},
    "routing": {
        "complexity": {"LOW": "cursor", "MEDIUM": "cursor", "HIGH": "codex"},
        "risk": {
            "LOW": {"plan": "none", "review": "none"},
            "MEDIUM": {"plan": "short", "review": "required"},
            "HIGH": {"plan": "formal_with_human_approval", "review": "required"},
        },
    },
}


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


def _write_policy(repo: Path, marker: str) -> None:
    policy = dict(_MIN_POLICY)
    policy["marker"] = marker
    path = repo / ".ai" / "policy.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")


def test_sanitized_env_strips_listed_keys() -> None:
    base = {
        "PATH": "/usr/bin",
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "GH_ENTERPRISE_TOKEN": "secret",
        "GITHUB_ENTERPRISE_TOKEN": "secret",
        "GIT_ASKPASS": "/bin/askpass",
        "SSH_AUTH_SOCK": "/tmp/ssh",
        "GH_CONFIG_DIR": "/tmp/gh",
        "KEEP_ME": "yes",
    }
    env = sanitized_env(base)
    for key in STRIP_ENV_KEYS:
        assert key not in env
    assert env["KEEP_ME"] == "yes"
    assert env["PATH"] == "/usr/bin"


def test_sanitized_env_keeps_path_from_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/custom/bin")
    monkeypatch.setenv("GH_TOKEN", "nope")
    env = sanitized_env()
    assert env["PATH"] == "/custom/bin"
    assert "GH_TOKEN" not in env


def test_load_policy_from_base_ignores_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_policy(repo, "base-A")
    _git(repo, "add", ".ai/policy.yml")
    _git(repo, "commit", "-m", "base policy")
    _git(repo, "checkout", "-b", "topic")
    _write_policy(repo, "branch-B")
    _git(repo, "add", ".ai/policy.yml")
    _git(repo, "commit", "-m", "weaken policy")
    loaded = load_policy_from_base(repo, base_ref="main")
    assert loaded["marker"] == "base-A"
    worktree = yaml.safe_load((repo / ".ai" / "policy.yml").read_text(encoding="utf-8"))
    assert worktree["marker"] == "branch-B"


def test_load_policy_from_base_missing_ref_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_policy(repo, "worktree-only")
    with pytest.raises(RuntimeError, match="no working-tree fallback"):
        load_policy_from_base(repo, base_ref="origin/main")


def test_control_policy_only_ok() -> None:
    result = check_control_changes([".ai/policy.yml"])
    assert result.ok is True
    assert result.touches_control is True
    assert result.control_files == [".ai/policy.yml"]
    assert result.unrelated_code_files == []


def test_control_with_tests_ok() -> None:
    result = check_control_changes([".ai/policy.yml", "tests/test_policy.py"])
    assert result.ok is True


def test_control_with_task_file_ok() -> None:
    result = check_control_changes([".ai/policy.yml", ".devflow/tasks/184.md"])
    assert result.ok is True


def test_control_with_src_not_ok() -> None:
    result = check_control_changes([".ai/policy.yml", "src/orders/service.py"])
    assert result.ok is False
    assert "src/orders/service.py" in result.message
    assert ".ai/policy.yml" in result.message


def test_verify_script_with_docs_ok() -> None:
    result = check_control_changes(["scripts/verify", "docs/testing/strategy.md"])
    assert result.ok is True
    assert result.touches_control is True


def test_no_control_files_ok() -> None:
    result = check_control_changes(["src/orders/service.py"])
    assert result.ok is True
    assert result.touches_control is False


def test_agent_output_flags_gh_pr_merge() -> None:
    findings = check_agent_output_for_violations("ran: gh pr merge 12\ndone")
    assert findings


def test_agent_output_clean_for_normal_code() -> None:
    findings = check_agent_output_for_violations("def add(a, b):\n    return a + b\n")
    assert findings == []


def test_control_paths_include_github_workflows() -> None:
    assert ".github/workflows/**" in CONTROL_PATHS
