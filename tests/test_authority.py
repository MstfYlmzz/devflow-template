from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from devflow.authority import (
    CONTROL_PATHS,
    STRIP_ENV_KEYS,
    check_agent_output_for_violations,
    check_control_changes,
    is_control_change,
    load_policy_from_base,
    sanitized_env,
)
from tests.conftest import git

_MIN_POLICY = {
    "floor": {},
    "signal_floor": {},
    "routing": {
        "complexity": {"LOW": "cursor", "MEDIUM": "cursor", "HIGH": "codex"},
        "risk": {
            "LOW": {"plan_approval": False, "review": False, "evidence": False},
            "MEDIUM": {"plan_approval": False, "review": True, "evidence": False},
            "HIGH": {"plan_approval": True, "review": True, "evidence": True},
        },
    },
}


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


def test_load_policy_from_base_ignores_worktree(git_repo: Path) -> None:
    repo = git_repo
    _write_policy(repo, "base-A")
    git(repo, "add", ".ai/policy.yml")
    git(repo, "commit", "-m", "base policy")
    git(repo, "checkout", "-b", "topic")
    _write_policy(repo, "branch-B")
    git(repo, "add", ".ai/policy.yml")
    git(repo, "commit", "-m", "weaken policy")
    loaded = load_policy_from_base(repo, base_ref="main")
    assert loaded["marker"] == "base-A"
    worktree = yaml.safe_load((repo / ".ai" / "policy.yml").read_text(encoding="utf-8"))
    assert worktree["marker"] == "branch-B"


def test_load_policy_from_base_missing_ref_errors(git_repo: Path) -> None:
    _write_policy(git_repo, "worktree-only")
    with pytest.raises(RuntimeError, match="no working-tree fallback"):
        load_policy_from_base(git_repo, base_ref="origin/main")


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


def test_control_with_devflow_source_ok() -> None:
    result = check_control_changes([".ai/policy.yml", "devflow/authority.py"])
    assert result.ok is True
    assert result.touches_control is True


def test_devflow_source_alone_is_control() -> None:
    result = check_control_changes(["devflow/policy.py"])
    assert result.touches_control is True
    assert result.ok is True


def test_control_with_src_not_ok() -> None:
    result = check_control_changes([".ai/policy.yml", "src/orders/service.py"])
    assert result.ok is False
    assert "src/orders/service.py" in result.message
    assert ".ai/policy.yml" in result.message


def test_devflow_with_src_not_ok() -> None:
    result = check_control_changes(["devflow/cli.py", "src/orders/service.py"])
    assert result.ok is False


def test_verify_script_with_docs_ok() -> None:
    result = check_control_changes(["scripts/verify", "docs/testing/strategy.md"])
    assert result.ok is True
    assert result.touches_control is True


def test_no_control_files_ok() -> None:
    result = check_control_changes(["src/orders/service.py"])
    assert result.ok is True
    assert result.touches_control is False


def test_template_policy_is_control() -> None:
    result = check_control_changes(["templates/project/.ai/policy.yml"])
    assert result.touches_control is True
    assert result.ok is True


def test_template_policy_with_devflow_ok() -> None:
    result = check_control_changes(
        ["templates/project/.ai/policy.yml", "devflow/policy.py"]
    )
    assert result.ok is True


def test_template_policy_with_src_not_ok() -> None:
    result = check_control_changes(
        ["templates/project/.ai/policy.yml", "src/orders/service.py"]
    )
    assert result.ok is False


def test_template_docs_with_devflow_ok() -> None:
    result = check_control_changes(
        ["templates/project/docs/testing/strategy.md", "devflow/cli.py"]
    )
    assert result.ok is True
    assert result.touches_control is True


def test_template_agents_md_alone_is_control() -> None:
    result = check_control_changes(["templates/project/AGENTS.md"])
    assert result.touches_control is True
    assert result.ok is True


def test_gitignore_alone_is_control() -> None:
    result = check_control_changes([".gitignore"])
    assert result.touches_control is True
    assert result.ok is True


def test_gitignore_with_devflow_ok() -> None:
    result = check_control_changes([".gitignore", "devflow/lock.py"])
    assert result.ok is True


def test_gitignore_with_src_not_ok() -> None:
    result = check_control_changes([".gitignore", "src/orders/service.py"])
    assert result.ok is False


def test_is_control_change_workflow_only() -> None:
    assert is_control_change([".github/workflows/x.yml"]) is True


def test_is_control_change_workflow_with_tests() -> None:
    assert is_control_change([".github/workflows/x.yml", "tests/test_x.py"]) is True


def test_is_control_change_template_docs_is_true() -> None:
    assert is_control_change(["templates/project/docs/testing/strategy.md"]) is True


def test_is_control_change_gitignore_is_true() -> None:
    assert is_control_change([".gitignore"]) is True


def test_is_control_change_src_is_false() -> None:
    assert is_control_change(["src/orders/service.py"]) is False


def test_is_control_change_empty_is_false() -> None:
    assert is_control_change([]) is False


def test_agent_output_flags_gh_pr_merge() -> None:
    findings = check_agent_output_for_violations("ran: gh pr merge 12\ndone")
    assert findings


def test_agent_output_clean_for_normal_code() -> None:
    findings = check_agent_output_for_violations("def add(a, b):\n    return a + b\n")
    assert findings == []


def test_control_paths_include_github_workflows() -> None:
    assert ".github/workflows/**" in CONTROL_PATHS
    assert "devflow/**" in CONTROL_PATHS
    assert "templates/project/**" in CONTROL_PATHS
    assert "templates/project/.ai/**" not in CONTROL_PATHS
    assert ".gitignore" in CONTROL_PATHS
