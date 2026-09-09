from __future__ import annotations

from pathlib import Path

import pytest

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.paths import resolve_task, task_path
from devflow.policy import ArchitectureImpact, Complexity, Risk
from devflow.runner import RunnerError, parse_triage_output, start
from devflow.states import State
from devflow.taskfile import append_section, create, read
from tests.conftest import git

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "templates" / "project"
_POLICY = (_TEMPLATES / ".ai" / "policy.yml").read_text(encoding="utf-8")
_ROLE_ROOT = (_ROOT / ".ai" / "roles" / "triage.md").read_text(encoding="utf-8")
_ROLE_TEMPLATE = (_TEMPLATES / ".ai" / "roles" / "triage.md").read_text(
    encoding="utf-8"
)


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


def _valid_yaml(
    *,
    complexity: str = "MEDIUM",
    architecture_impact: str = "NONE",
    uncertain: str = "false",
) -> str:
    return (
        "```yaml\n"
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        f"complexity: {complexity}\n"
        f"architecture_impact: {architecture_impact}\n"
        f"uncertain: {uncertain}\n"
        "```\n"
    )


def _agent_returning(output: str):
    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
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
        if "plan_only: true" in prompt_file.read_text(encoding="utf-8"):
            return AgentResult(AgentStatus.OK, "- do the work\n", None, 0.3)
        return AgentResult(AgentStatus.OK, output, None, 0.4)

    return run


def test_triage_roles_forbid_union_placeholders() -> None:
    assert _ROLE_ROOT == _ROLE_TEMPLATE
    for text in (_ROLE_ROOT, _ROLE_TEMPLATE):
        assert "LOW | MEDIUM | HIGH" not in text
        assert "NONE | POSSIBLE | YES" not in text
        assert "choose exactly one" in text.casefold() or "Allowed values" in text
        assert "complexity: MEDIUM" in text
        assert "architecture_impact: NONE" in text
        assert "If only missing fields were requested" not in text
        assert "Always emit the complete YAML schema" in text
        assert "still return every output field" in text
        assert "- `uncertain`" in text


def test_parse_rejects_complexity_union_placeholder() -> None:
    raw = (
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "complexity: LOW | MEDIUM | HIGH\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
    )
    with pytest.raises(
        RunnerError, match=r"invalid triage complexity: LOW \| MEDIUM \| HIGH"
    ):
        parse_triage_output(raw)


def test_parse_rejects_architecture_union_placeholder() -> None:
    raw = (
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "complexity: MEDIUM\n"
        "architecture_impact: NONE | POSSIBLE | YES\n"
        "uncertain: false\n"
    )
    with pytest.raises(
        RunnerError,
        match=r"invalid triage architecture_impact: NONE \| POSSIBLE \| YES",
    ):
        parse_triage_output(raw)


def test_parse_accepts_valid_output() -> None:
    signals, complexity, impact, uncertain = parse_triage_output(_valid_yaml())
    assert complexity is Complexity.MEDIUM
    assert impact is ArchitectureImpact.NONE
    assert uncertain is False
    assert signals.transaction_change is False


def test_parse_normalizes_lowercase_enums() -> None:
    signals, complexity, impact, uncertain = parse_triage_output(
        _valid_yaml(complexity="medium", architecture_impact="possible")
    )
    assert complexity is Complexity.MEDIUM
    assert impact is ArchitectureImpact.POSSIBLE
    assert uncertain is False
    assert signals.unfamiliar_area is False


def test_parse_rejects_string_booleans() -> None:
    with pytest.raises(RunnerError, match="invalid triage uncertain"):
        parse_triage_output(_valid_yaml(uncertain='"false"'))
    raw = (
        "signals:\n"
        '  transaction_change: "false"\n'
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "complexity: MEDIUM\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
    )
    with pytest.raises(RunnerError, match="invalid triage signals.transaction_change"):
        parse_triage_output(raw)


def test_runner_malformed_triage_blocks(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(task_path(project, 27), 27, "Bad triage", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    bad = (
        "```yaml\n"
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "complexity: LOW | MEDIUM | HIGH\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
        "```\n"
    )
    monkeypatch.setattr("devflow.agents.run", _agent_returning(bad))
    result = start(project, 27)
    assert result.final_state is State.BLOCKED
    assert any("blocked: TRIAGE_INVALID_OUTPUT" in item for item in result.messages)
    assert any("TRIAGE -> BLOCKED" in item for item in result.messages)
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert tf.frontmatter.state == "BLOCKED"
    assert tf.frontmatter.blocked_reason == "TRIAGE_INVALID_OUTPUT"
    assert tf.frontmatter.complexity_proposed is None
    assert tf.frontmatter.signals is None
    assert tf.frontmatter.architecture_impact is None
    assert "invalid triage complexity: LOW | MEDIUM | HIGH" in tf.body
    assert "Triage raw output" in tf.body
    assert "LOW | MEDIUM | HIGH" in tf.body


def test_runner_valid_triage_continues(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(task_path(project, 27), 27, "Good triage", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    monkeypatch.setattr("devflow.agents.run", _agent_returning(_valid_yaml()))
    result = start(project, 27, risk_hint=Risk.LOW)
    assert result.final_state is not State.BLOCKED
    assert result.final_state is not State.TRIAGE
    assert result.final_state is not State.BACKLOG
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert tf.frontmatter.complexity_proposed is Complexity.MEDIUM
    assert tf.frontmatter.architecture_impact is ArchitectureImpact.NONE
    assert tf.frontmatter.blocked_reason is None


def test_runner_accepts_complete_schema_matching_task24_shape(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Task 24 failure omitted ``uncertain``; complete schema must pass."""
    create(task_path(project, 27), 27, "Control path coverage", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    complete = (
        "```yaml\n"
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: true\n"
        "  unfamiliar_area: false\n"
        "complexity: MEDIUM\n"
        "architecture_impact: POSSIBLE\n"
        "uncertain: false\n"
        "```\n"
    )
    monkeypatch.setattr("devflow.agents.run", _agent_returning(complete))
    result = start(project, 27)
    assert result.final_state is State.PLAN_APPROVAL
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert tf.frontmatter.complexity_proposed is Complexity.MEDIUM
    assert tf.frontmatter.architecture_impact is ArchitectureImpact.POSSIBLE
    assert tf.frontmatter.uncertain is False
    assert tf.frontmatter.blocked_reason is None


def test_start_still_rejects_non_backlog_after_block(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(task_path(project, 27), 27, "No resume", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    bad = (
        "```yaml\n"
        "signals:\n"
        "  transaction_change: false\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "complexity: LOW | MEDIUM | HIGH\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
        "```\n"
    )
    monkeypatch.setattr("devflow.agents.run", _agent_returning(bad))
    first = start(project, 27)
    assert first.final_state is State.BLOCKED
    with pytest.raises(RunnerError, match="expected BACKLOG"):
        start(project, 27)
