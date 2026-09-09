from __future__ import annotations

from pathlib import Path

import pytest

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.authority import check_agent_output_for_violations
from devflow.paths import resolve_task, task_path
from devflow.policy import Complexity, Risk, plan_detail_for
from devflow.runner import start
from devflow.states import State
from devflow.taskfile import append_section, body_sections, create, read
from tests.conftest import git

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "templates" / "project"
_POLICY = (_TEMPLATES / ".ai" / "policy.yml").read_text(encoding="utf-8")
_IMPL_ROOT = (_ROOT / ".ai" / "roles" / "implementer.md").read_text(encoding="utf-8")
_IMPL_TEMPLATE = (_TEMPLATES / ".ai" / "roles" / "implementer.md").read_text(
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


_VALID_TRIAGE = (
    "```yaml\n"
    "signals:\n"
    "  transaction_change: false\n"
    "  concurrency_sensitive: false\n"
    "  architecture_boundary_change: false\n"
    "  unfamiliar_area: false\n"
    "complexity: MEDIUM\n"
    "architecture_impact: NONE\n"
    "uncertain: false\n"
    "```\n"
)

_VALID_PLAN = "- inspect authority\n- add minimal test\n"


def _split_agent(
    *,
    triage_stdout: str,
    triage_stderr: str = "",
    plan_stdout: str = _VALID_PLAN,
    plan_stderr: str = "",
):
    """Simulate agents.run where only stdout is semantic (post-fix contract)."""

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
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
        if "plan_only: true" in text or "plan_detail:" in text:
            # Runner only receives stdout as AgentResult.output.
            _ = plan_stderr
            return AgentResult(AgentStatus.OK, plan_stdout, None, 0.3)
        _ = triage_stderr
        return AgentResult(AgentStatus.OK, triage_stdout, None, 0.4)

    return run


def test_implementer_role_follows_plan_detail_not_risk() -> None:
    assert _IMPL_ROOT == _IMPL_TEMPLATE
    for text in (_IMPL_ROOT, _IMPL_TEMPLATE):
        assert "Match plan detail to risk" not in text
        assert "plan_detail" in text
        assert "Do not derive plan depth from risk" in text
        assert "Risk controls approval" in text


def test_policy_high_risk_medium_complexity_stays_brief() -> None:
    assert plan_detail_for(Complexity.MEDIUM) == "brief"
    assert plan_detail_for(Complexity.HIGH) == "formal"
    assert plan_detail_for(Complexity.LOW) == "none"


def test_authority_ignores_forbidden_commands_in_stderr_only_output() -> None:
    clean = "FINAL ANSWER\n"
    assert check_agent_output_for_violations(clean) == []
    # Simulated post-fix AgentResult.output never includes stderr.
    assert "gh pr merge" not in clean


def test_authority_still_flags_forbidden_commands_in_stdout() -> None:
    findings = check_agent_output_for_violations("please run: gh pr merge 12\n")
    assert findings


def test_triage_ignores_valid_yaml_on_stderr(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(task_path(project, 27), 27, "Stderr trap", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    # Only invalid stdout is visible to the runner; stderr YAML must not rescue.
    monkeypatch.setattr(
        "devflow.agents.run",
        _split_agent(
            triage_stdout="not yaml at all\n",
            triage_stderr=(
                "OpenAI Codex...\n"
                "```yaml\n"
                "signals:\n"
                "  transaction_change: false\n"
                "  concurrency_sensitive: false\n"
                "  architecture_boundary_change: false\n"
                "  unfamiliar_area: false\n"
                "complexity: MEDIUM\n"
                "architecture_impact: NONE\n"
                "uncertain: false\n"
                "```\n"
            ),
        ),
    )
    result = start(project, 27)
    assert result.final_state is State.BLOCKED
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert tf.frontmatter.blocked_reason == "TRIAGE_INVALID_OUTPUT"
    assert tf.frontmatter.complexity_proposed is None


def test_triage_uses_stdout_despite_noisy_stderr(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(task_path(project, 27), 27, "Stdout triage", modules=["src/app.py"])
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    monkeypatch.setattr(
        "devflow.agents.run",
        _split_agent(
            triage_stdout=_VALID_TRIAGE,
            triage_stderr="OpenAI Codex...\nprompt\ngh pr merge\n",
        ),
    )
    result = start(project, 27, risk_hint=Risk.HIGH)
    assert result.final_state is State.PLAN_APPROVAL
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert tf.frontmatter.complexity_proposed is Complexity.MEDIUM
    assert "OpenAI Codex" not in tf.body
    assert "gh pr merge" not in tf.body


def test_plan_journal_excludes_codex_transcript(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(
        task_path(project, 27),
        27,
        "Plan clean",
        modules=["src/app.py"],
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
    )
    git("add", "-A", cwd=project)
    git("commit", "-m", "task", cwd=project)
    _origin_main(project)
    monkeypatch.setattr(
        "devflow.agents.run",
        _split_agent(
            triage_stdout=_VALID_TRIAGE,
            plan_stdout=_VALID_PLAN,
            plan_stderr=(
                "OpenAI Codex v0\nworkdir: C:/tmp\ninstructions\nexec\ntokens used\n"
            ),
        ),
    )
    result = start(project, 27)
    assert result.final_state is State.PLAN_APPROVAL
    active = resolve_task(project, 27)
    assert active is not None
    tf = read(active)
    assert "Plan" in body_sections(tf)
    assert "- inspect authority" in tf.body
    assert "- add minimal test" in tf.body
    assert "OpenAI Codex" not in tf.body
    assert "workdir:" not in tf.body
    assert "tokens used" not in tf.body
