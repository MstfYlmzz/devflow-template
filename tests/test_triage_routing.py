from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.paths import task_path
from devflow.policy import (
    Risk,
    load_policy,
    triage_provider,
    validate_policy,
)
from devflow.runner import start
from devflow.states import State
from devflow.taskfile import append_section, create, read
from tests.conftest import git

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "templates" / "project"
_POLICY = (_TEMPLATES / ".ai" / "policy.yml").read_text(encoding="utf-8")


def _origin_main(repo: Path) -> None:
    sha = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    git("update-ref", "refs/remotes/origin/main", sha, cwd=repo)


def _seed(repo: Path) -> None:
    ai = repo / ".ai" / "roles"
    ai.mkdir(parents=True)
    (repo / ".ai" / "policy.yml").write_text(
        _POLICY.replace(
            "reviewed_for_this_project: false",
            "reviewed_for_this_project: true",
            1,
        ),
        encoding="utf-8",
    )
    for path in (_TEMPLATES / ".ai" / "roles").iterdir():
        (ai / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "docs" / "requirements").mkdir(parents=True)
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / ".gitignore").write_text(
        ".devflow/worktrees/\n.devflow/locks/\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    _origin_main(repo)


@pytest.fixture
def project(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed(git_repo)
    monkeypatch.setattr("devflow.runner.run_verify", lambda _wt: (True, "ok\n"))
    return git_repo


def _commit_policy(repo: Path) -> None:
    git("add", ".ai/policy.yml", cwd=repo)
    git("commit", "-m", "update policy", cwd=repo)
    _origin_main(repo)


def _set_routing(repo: Path, **fields: object) -> dict[str, object]:
    path = repo / ".ai" / "policy.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    routing = data.setdefault("routing", {})
    routing.update(fields)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _commit_policy(repo)
    return data


def _triage_yaml(*, complexity: str = "MEDIUM") -> str:
    return (
        "```yaml\n"
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        f"complexity: {complexity}\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
        "```\n"
    )


def _tracking_agent(calls: list[tuple[str, AgentMode]], *, complexity: str = "MEDIUM"):
    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        calls.append((agent, mode))
        text = prompt_file.read_text(encoding="utf-8")
        if mode is AgentMode.EDIT:
            (worktree / "src" / "app.py").write_text(
                "print('done')\n", encoding="utf-8"
            )
            path = task_path(worktree, 27)
            if path.is_file() and "Doc impact" not in read(path).body:
                append_section(path, "Doc impact", "status: none\nfiles: []\n")
            return AgentResult(AgentStatus.OK, "implemented", None, 1.0)
        if mode is AgentMode.REVIEW:
            return AgentResult(AgentStatus.OK, "[]", None, 0.5)
        if "plan_only: true" in text:
            return AgentResult(AgentStatus.OK, "- do the work\n", None, 0.3)
        return AgentResult(
            AgentStatus.OK, _triage_yaml(complexity=complexity), None, 0.4
        )

    return run


def test_root_bootstrap_policy_triage_is_codex() -> None:
    policy = load_policy(_ROOT / ".ai" / "policy.yml")
    assert validate_policy(policy) == []
    assert triage_provider(policy) == "codex"
    assert policy["routing"]["complexity"] == {
        "LOW": "codex",
        "MEDIUM": "codex",
        "HIGH": "codex",
    }


def test_template_policy_triage_is_cursor() -> None:
    policy = load_policy(_TEMPLATES / ".ai" / "policy.yml")
    assert policy["routing"]["triage"] == "cursor"
    assert policy["routing"]["complexity"] == {
        "LOW": "cursor",
        "MEDIUM": "cursor",
        "HIGH": "codex",
    }


def test_missing_triage_is_invalid() -> None:
    policy = copy.deepcopy(load_policy(_TEMPLATES / ".ai" / "policy.yml"))
    policy["reviewed_for_this_project"] = True
    del policy["routing"]["triage"]
    errors = validate_policy(policy)
    assert any("routing.triage is required" in item for item in errors)


def test_unknown_triage_provider_is_invalid() -> None:
    policy = copy.deepcopy(load_policy(_TEMPLATES / ".ai" / "policy.yml"))
    policy["reviewed_for_this_project"] = True
    policy["routing"]["triage"] = "banana"
    errors = validate_policy(policy)
    assert any("routing.triage unknown agent: banana" in item for item in errors)


def test_runner_uses_policy_triage_codex(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_routing(project, triage="codex")
    create(task_path(project, 27), 27, "Triage codex", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    calls: list[tuple[str, AgentMode]] = []
    monkeypatch.setattr("devflow.agents.run", _tracking_agent(calls))
    result = start(project, 27, risk_hint=Risk.LOW)
    assert calls
    assert calls[0] == ("codex", AgentMode.READ_ONLY)
    assert any("triage (codex, read_only)" in item for item in result.messages)
    assert result.final_state is not State.BACKLOG


def test_runner_uses_policy_triage_claude(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_routing(project, triage="claude")
    create(task_path(project, 27), 27, "Triage claude", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    calls: list[tuple[str, AgentMode]] = []
    monkeypatch.setattr("devflow.agents.run", _tracking_agent(calls))
    start(project, 27, risk_hint=Risk.LOW)
    assert calls[0] == ("claude", AgentMode.READ_ONLY)


def test_triage_unavailable_no_fallback(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_routing(project, triage="cursor")
    create(task_path(project, 27), 27, "No fallback", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    calls: list[tuple[str, AgentMode]] = []

    def fake(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        calls.append((agent, mode))
        return AgentResult(
            AgentStatus.BLOCKED,
            "",
            f"{agent} command not configured (set DEVFLOW_CURSOR_CMD)",
            0.1,
        )

    monkeypatch.setattr("devflow.agents.run", fake)
    result = start(project, 27)
    assert calls == [("cursor", AgentMode.READ_ONLY)]
    assert result.final_state is State.BLOCKED
    assert result.worktree is not None
    tf = read(task_path(result.worktree, 27))
    assert tf.frontmatter.blocked_reason == "AGENT_BLOCKED"


def test_implementer_independent_of_triage(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_routing(
        project,
        triage="cursor",
        complexity={"LOW": "cursor", "MEDIUM": "cursor", "HIGH": "codex"},
    )
    create(task_path(project, 27), 27, "High after triage", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    calls: list[tuple[str, AgentMode]] = []
    monkeypatch.setattr("devflow.agents.run", _tracking_agent(calls, complexity="HIGH"))
    result = start(project, 27, risk_hint=Risk.LOW)
    assert calls[0] == ("cursor", AgentMode.READ_ONLY)
    implementers = [agent for agent, mode in calls[1:] if mode is AgentMode.EDIT]
    assert implementers
    assert implementers[0] == "codex"
    assert result.final_state is State.READY_TO_MERGE
    assert any("triage (cursor, read_only)" in item for item in result.messages)


def test_issue_backed_uses_policy_triage_codex(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import subprocess
    from typing import Any

    _set_routing(project, triage="codex")
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv and argv[0] == "gh":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "number": 27,
                        "title": "Add cancellation guard",
                        "body": "Prevent duplicate cancellation.",
                        "state": "OPEN",
                    }
                ),
                stderr="",
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    calls: list[tuple[str, AgentMode]] = []
    monkeypatch.setattr("devflow.agents.run", _tracking_agent(calls))
    result = start(project, 27, risk_hint=Risk.HIGH)
    assert calls
    assert calls[0] == ("codex", AgentMode.READ_ONLY)
    assert any("triage (codex, read_only)" in item for item in result.messages)
    assert result.worktree is not None
    assert task_path(result.worktree, 27).is_file()
