from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.gitops import ensure_task_worktree, inspect_resume, task_worktree
from devflow.lock import acquire, lock_path, read_lock
from devflow.policy import Complexity, Risk
from devflow.runner import RunnerError, approve, cancel, start, stop
from devflow.states import State
from devflow.taskfile import append_section, create, read
from tests.conftest import git

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "project"
_POLICY = (_TEMPLATES / ".ai" / "policy.yml").read_text(encoding="utf-8")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.pid is not None
    return proc.pid


def _origin_main(repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "update-ref", "refs/remotes/origin/main", sha)


def _seed(repo: Path) -> None:
    ai = repo / ".ai" / "roles"
    ai.mkdir(parents=True)
    (repo / ".ai" / "policy.yml").write_text(_POLICY, encoding="utf-8")
    for path in (_TEMPLATES / ".ai" / "roles").iterdir():
        (ai / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "docs" / "requirements").mkdir(parents=True)
    (repo / "docs" / "adr").mkdir(parents=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    _origin_main(repo)


def _task(repo: Path, **fields: object) -> Path:
    path = repo / ".devflow" / "tasks" / "184.md"
    create(path, 184, "Order cancel", **fields)  # type: ignore[arg-type]
    return path


def _epic(**fields: object) -> dict[str, object]:
    data: dict[str, object] = {
        "risk_proposed": Risk.MEDIUM,
        "complexity_proposed": Complexity.MEDIUM,
        "modules": ["src/app.py"],
    }
    data.update(fields)
    return data


def _ok_agent(task_path: Path, review_yaml: str | None = None):
    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        on_spawn = kwargs.get("on_spawn")
        if callable(on_spawn):
            on_spawn(os.getpid())
        if mode is AgentMode.EDIT:
            (worktree / "src" / "app.py").write_text(
                "print('done')\n", encoding="utf-8"
            )
            body = read(task_path).body
            if "Doc impact" not in body:
                append_section(task_path, "Doc impact", "status: none\nfiles: []\n")
            return AgentResult(AgentStatus.OK, "implemented", None, 1.5)
        if mode is AgentMode.REVIEW:
            output = review_yaml or "[]"
            return AgentResult(AgentStatus.OK, output, None, 0.8)
        return AgentResult(
            AgentStatus.OK,
            (
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
            None,
            0.4,
        )

    return run


@pytest.fixture
def project(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed(git_repo)
    monkeypatch.setattr("devflow.runner.run_verify", lambda _wt: (True, "ok\n"))
    return git_repo


def test_start_rejects_non_backlog(project: Path) -> None:
    _task(project, state="IMPLEMENTING")
    with pytest.raises(RunnerError, match="IMPLEMENTING"):
        start(project, 184)


def test_lock_held_stops_with_message(project: Path) -> None:
    _task(project, **_epic())
    acquire(184, "start", project)
    result = start(project, 184)
    assert any("already running" in item for item in result.messages)
    assert result.final_state is State.BACKLOG
    assert read_lock(184, project) is not None


def test_stale_lock_suggests_recover(project: Path) -> None:
    _task(project, **_epic())
    path = lock_path(184, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": 184,
                "pid": _dead_pid(),
                "started_at": "2026-09-08T14:02:00+03:00",
                "host": "test",
                "stage": "start",
                "agent_pid": None,
            }
        ),
        encoding="utf-8",
    )
    result = start(project, 184)
    joined = "\n".join(result.messages)
    assert "stale lock" in joined
    assert "devflow recover 184" in joined
    assert path.is_file()


def test_blocked_by_unmerged_stops(project: Path) -> None:
    create(project / ".devflow" / "tasks" / "1.md", 1, "Blocker", state="IMPLEMENTING")
    _task(project, blocked_by=[1], **_epic())
    with pytest.raises(RunnerError, match="blocked by unmerged tasks: 1"):
        start(project, 184)


def test_policy_comes_from_origin_main(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = project / ".ai" / "policy.yml"
    base = policy_path.read_text(encoding="utf-8")
    weakened = base.replace("HIGH: codex", "HIGH: cursor")
    policy_path.write_text(weakened, encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-m", "weaken")
    _task(
        project,
        risk_proposed=Risk.LOW,
        complexity_proposed=Complexity.HIGH,
        modules=["src/app.py"],
    )
    seen: list[str] = []

    def fake(agent: str, *args: object, **kwargs: object) -> AgentResult:
        seen.append(agent)
        return AgentResult(AgentStatus.BLOCKED, "", "stop", 0.1)

    monkeypatch.setattr("devflow.agents.run", fake)
    result = start(project, 184)
    assert seen == ["codex"]
    assert result.final_state is State.BLOCKED


def test_skip_review_without_reason_errors(project: Path) -> None:
    _task(project, **_epic())
    with pytest.raises(RunnerError, match="--reason is required"):
        start(project, 184, skip_review=True)


def test_high_epic_stops_at_plan_approval(project: Path) -> None:
    _task(
        project,
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    result = start(project, 184)
    assert result.final_state is State.PLAN_APPROVAL
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.state == "PLAN_APPROVAL"
    assert result.worktree is None
    assert any("devflow approve 184" in item for item in result.messages)


def test_no_epic_runs_triage(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _task(project, modules=["src/app.py"])
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    result = start(project, 184)
    assert any("triage (cursor, read_only)" in item for item in result.messages)
    assert result.final_state is not State.BACKLOG


def test_agent_blocked_does_not_call_second_provider(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task(project, **_epic())
    seen: list[str] = []

    def fake(agent: str, *args: object, **kwargs: object) -> AgentResult:
        seen.append(agent)
        return AgentResult(AgentStatus.BLOCKED, "", "unavailable", 0.2)

    monkeypatch.setattr("devflow.agents.run", fake)
    result = start(project, 184)
    assert seen == ["cursor"]
    assert result.final_state is State.BLOCKED
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.blocked_reason == "IMPLEMENTER_UNAVAILABLE"


def test_verify_failure_stays_implementing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    monkeypatch.setattr("devflow.runner.run_verify", lambda _wt: (False, "boom\n"))
    result = start(project, 184)
    assert result.final_state is State.IMPLEMENTING
    assert result.verify_passed is False
    assert result.worktree is not None
    assert result.worktree.is_dir()
    assert read_lock(184, project) is None


def test_rebase_conflict_blocks_and_keeps_worktree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))

    def boom(_worktree: Path, _base: str) -> str:
        raise RuntimeError("conflict")

    monkeypatch.setattr("devflow.gitops.rebase_onto_base", boom)
    result = start(project, 184)
    assert result.final_state is State.BLOCKED
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.blocked_reason == "REBASE_CONFLICT"
    assert result.worktree is not None
    assert result.worktree.is_dir()


def test_merge_gate_raise_goes_to_triage(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        if mode is AgentMode.EDIT:
            auth = worktree / "src" / "auth"
            auth.mkdir(parents=True, exist_ok=True)
            (auth / "login.py").write_text("x = 1\n", encoding="utf-8")
            append_section(path, "Doc impact", "status: none\nfiles: []\n")
        return AgentResult(AgentStatus.OK, "ok", None, 0.5)

    monkeypatch.setattr("devflow.agents.run", run)
    result = start(project, 184)
    assert result.final_state is State.TRIAGE


def test_review_records_head_and_base(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    findings = "```yaml\nid: F1\nseverity: HIGH\nevidence: test\nproblem: bug\n```\n"
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, findings))
    result = start(project, 184)
    assert result.review_record is not None
    assert result.review_record.head_sha
    assert result.review_record.base_sha
    tf = read(path)
    assert tf.frontmatter.review_records
    assert tf.frontmatter.review_records[0].head_sha == result.review_record.head_sha
    assert result.final_state is State.REWORK


def test_unverified_high_does_not_rework(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    findings = "```yaml\nid: F1\nseverity: HIGH\nproblem: maybe\n```\n"
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, findings))
    result = start(project, 184)
    assert result.final_state is not State.REWORK
    assert result.review_record is not None
    assert result.review_record.unverified_high == 1
    assert result.review_record.blocking_findings == 0
    assert any("merge gate: blocked" in item for item in result.messages)


def test_existing_worktree_is_reused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    (wt / "keep.txt").write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    result = start(project, 184)
    assert result.worktree == wt
    assert (wt / "keep.txt").is_file()
    ctx = inspect_resume(184, project)
    assert ctx.worktree_exists is True


def test_dry_run_makes_no_changes(project: Path) -> None:
    path = _task(project, **_epic())
    before = path.read_text(encoding="utf-8")
    result = start(project, 184, dry_run=True)
    assert result.final_state is State.BACKLOG
    assert result.worktree is None
    assert read_lock(184, project) is None
    assert not task_worktree(project, 184).exists()
    assert path.read_text(encoding="utf-8") == before
    assert any("dry-run" in item for item in result.messages)


def test_exception_releases_lock(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task(project, **_epic())

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("policy boom")

    monkeypatch.setattr("devflow.runner.load_policy_from_base", boom)
    with pytest.raises(RuntimeError, match="policy boom"):
        start(project, 184)
    assert read_lock(184, project) is None


def test_stop_sets_blocked_keeps_worktree(project: Path) -> None:
    _task(project, state="IMPLEMENTING", **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    acquire(184, "start", project)
    result = stop(project, 184)
    assert result.final_state is State.BLOCKED
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.blocked_reason == "USER_STOPPED"
    assert wt.is_dir()
    assert read_lock(184, project) is None


def test_cancel_with_reason_keeps_worktree(project: Path) -> None:
    _task(project, **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    result = cancel(project, 184, reason="out of scope")
    assert result.final_state is State.CANCELLED
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.state == "CANCELLED"
    assert "out of scope" in tf.body
    assert wt.is_dir()
    assert (project / ".devflow" / "tasks" / "184.md").is_file()


def test_cancel_discard_removes_worktree_keeps_file(project: Path) -> None:
    _task(project, **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    cancel(project, 184, reason="drop it", discard=True)
    assert not wt.exists()
    assert (project / ".devflow" / "tasks" / "184.md").is_file()
    tf = read(project / ".devflow" / "tasks" / "184.md")
    assert tf.frontmatter.state == "CANCELLED"


def test_cancel_requires_reason(project: Path) -> None:
    _task(project, **_epic())
    with pytest.raises(RunnerError, match="--reason is required"):
        cancel(project, 184, reason="  ")


def test_cancel_merged_errors(project: Path) -> None:
    _task(project, state="MERGED")
    with pytest.raises(RunnerError, match="MERGED"):
        cancel(project, 184, reason="nope")


def test_stop_merged_errors(project: Path) -> None:
    _task(project, state="MERGED")
    with pytest.raises(RunnerError, match="MERGED"):
        stop(project, 184)


def test_approve_continues_from_plan_approval(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(
        project,
        state="PLAN_APPROVAL",
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    result = approve(project, 184, skip_review=True, reason="ship it")
    assert result.final_state is not State.PLAN_APPROVAL
    assert result.worktree is not None
