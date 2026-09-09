from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.gitops import (
    ensure_task_worktree,
    inspect_resume,
    rebase_onto_base,
    remove_task_worktree,
    task_worktree,
)
from devflow.lock import acquire, lock_path, read_lock
from devflow.paths import resolve_task
from devflow.policy import Complexity, Risk
from devflow.runner import (
    RunnerError,
    WorktreeSetupError,
    _worktree_script_argv,
    approve,
    cancel,
    resolve_git_bash,
    resume,
    run_setup_worktree,
    run_verify,
    start,
    stop,
)
from devflow.states import State
from devflow.taskfile import append_section, body_sections, create, read
from tests.conftest import git

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "project"
_POLICY = (_TEMPLATES / ".ai" / "policy.yml").read_text(encoding="utf-8")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.pid is not None
    return proc.pid


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
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    _origin_main(repo)


def _task(repo: Path, **fields: object) -> Path:
    path = repo / ".devflow" / "tasks" / "184.md"
    create(path, 184, "Order cancel", **fields)  # type: ignore[arg-type]
    return path


def _active(repo: Path, task_id: int = 184) -> Path:
    found = resolve_task(repo, task_id)
    assert found is not None
    return found


def _epic(**fields: object) -> dict[str, object]:
    data: dict[str, object] = {
        "risk_proposed": Risk.MEDIUM,
        "complexity_proposed": Complexity.MEDIUM,
        "modules": ["src/app.py"],
    }
    data.update(fields)
    return data


def _ok_agent(task_file_path: Path, review_yaml: str | None = None):
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
        prompt = prompt_file.read_text(encoding="utf-8")
        if mode is AgentMode.EDIT:
            (worktree / "src" / "app.py").write_text(
                "print('done')\n", encoding="utf-8"
            )
            active = worktree / ".devflow" / "tasks" / task_file_path.name
            target = active if active.is_file() else task_file_path
            body = read(target).body
            if "Doc impact" not in body:
                append_section(target, "Doc impact", "status: none\nfiles: []\n")
            return AgentResult(AgentStatus.OK, "implemented", None, 1.5)
        if mode is AgentMode.REVIEW:
            output = review_yaml or "[]"
            return AgentResult(AgentStatus.OK, output, None, 0.8)
        if "plan_only: true" in prompt:
            return AgentResult(AgentStatus.OK, "- do the work\n", None, 0.3)
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
    git("add", "-A", cwd=project)
    git("commit", "-m", "weaken", cwd=project)
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


def test_high_epic_stops_at_plan_approval(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(
        project,
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    modes: list[AgentMode] = []

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        modes.append(mode)
        return _ok_agent(path)(agent, prompt_file, worktree, mode, **kwargs)

    monkeypatch.setattr("devflow.agents.run", run)
    result = start(project, 184)
    assert result.final_state is State.PLAN_APPROVAL
    tf = read(_active(project))
    assert tf.frontmatter.state == "PLAN_APPROVAL"
    assert result.worktree is not None
    assert result.worktree.is_dir()
    assert modes == [AgentMode.READ_ONLY]
    assert AgentMode.EDIT not in modes
    assert "Plan" in body_sections(tf)
    assert "- do the work" in tf.body
    assert any("devflow approve 184" in item for item in result.messages)


def test_start_records_floor_from_module(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(
        project,
        risk_proposed=Risk.MEDIUM,
        complexity_proposed=Complexity.MEDIUM,
        modules=["auth"],
    )
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    start(project, 184)
    tf = read(_active(project))
    assert tf.frontmatter.floor_risk is Risk.HIGH
    assert tf.frontmatter.floor_matched
    assert any("from module: auth" in item for item in tf.frontmatter.floor_matched)
    assert all(" (path: " not in item for item in tf.frontmatter.floor_matched)


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
    tf = read(_active(project))
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
    joined = "\n".join(result.messages)
    assert "verify... FAIL" in joined
    assert "--- verify output (last 20 lines) ---" in joined
    assert "boom" in joined
    tf = read(_active(project))
    assert "boom" in tf.body


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
    tf = read(_active(project))
    assert tf.frontmatter.blocked_reason == "REBASE_CONFLICT"
    assert result.worktree is not None
    assert result.worktree.is_dir()


def test_merge_gate_raise_goes_to_triage(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task(project, **_epic())

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
            active = worktree / ".devflow" / "tasks" / "184.md"
            append_section(active, "Doc impact", "status: none\nfiles: []\n")
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
    tf = read(_active(project))
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
    assert any("merge readiness: blocked" in item for item in result.messages)


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


def test_dry_run_warns_when_providers_unconfigured(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task(project, **_epic())
    monkeypatch.delenv("DEVFLOW_CURSOR_CMD", raising=False)
    monkeypatch.delenv("DEVFLOW_CLAUDE_CMD", raising=False)
    result = start(project, 184, dry_run=True)
    assert result.final_state is State.BACKLOG
    joined = "\n".join(result.messages)
    assert "would run implementer (cursor, edit)" in joined
    assert "WARNING — cursor is not configured (set DEVFLOW_CURSOR_CMD)" in joined
    assert "a real run would stop with BLOCKED: IMPLEMENTER_UNAVAILABLE" in joined
    assert "would run reviewer (claude, review)" in joined
    assert "WARNING — claude is not configured (set DEVFLOW_CLAUDE_CMD)" in joined
    assert "a real run would stop with BLOCKED: REVIEWER_UNAVAILABLE" in joined


def test_dry_run_skips_provider_warning_when_configured(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task(project, **_epic())
    monkeypatch.setenv("DEVFLOW_CURSOR_CMD", "cursor-agent")
    monkeypatch.setenv("DEVFLOW_CLAUDE_CMD", "claude")
    result = start(project, 184, dry_run=True)
    joined = "\n".join(result.messages)
    assert "WARNING" not in joined
    assert "IMPLEMENTER_UNAVAILABLE" not in joined


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
    tf = read(_active(project))
    assert tf.frontmatter.blocked_reason == "USER_STOPPED"
    assert wt.is_dir()
    assert read_lock(184, project) is None


def test_cancel_with_reason_keeps_worktree(project: Path) -> None:
    _task(project, **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    result = cancel(project, 184, reason="out of scope")
    assert result.final_state is State.CANCELLED
    tf = read(_active(project))
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
    tf = read(_active(project))
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
    append_section(path, "Plan", "- do X\n")
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    result = approve(project, 184, skip_review=True, reason="ship it")
    assert result.final_state is not State.PLAN_APPROVAL
    assert result.worktree is not None


def test_approve_without_plan_errors(project: Path) -> None:
    _task(
        project,
        state="PLAN_APPROVAL",
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    with pytest.raises(RunnerError, match="task 184 has no plan to approve"):
        approve(project, 184, skip_review=True, reason="ship it")


def test_medium_writes_plan_then_implements(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    modes: list[AgentMode] = []

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        modes.append(mode)
        return _ok_agent(path)(agent, prompt_file, worktree, mode, **kwargs)

    monkeypatch.setattr("devflow.agents.run", run)
    result = start(project, 184)
    assert result.final_state is not State.PLAN_APPROVAL
    assert AgentMode.READ_ONLY in modes
    assert AgentMode.EDIT in modes
    assert modes.index(AgentMode.READ_ONLY) < modes.index(AgentMode.EDIT)
    tf = read(_active(project))
    assert "Plan" in body_sections(tf)
    assert "- do the work" in tf.body


def test_low_skips_plan_generation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(
        project,
        risk_proposed=Risk.LOW,
        complexity_proposed=Complexity.LOW,
        modules=["src/app.py"],
    )
    modes: list[AgentMode] = []

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        modes.append(mode)
        return _ok_agent(path)(agent, prompt_file, worktree, mode, **kwargs)

    monkeypatch.setattr("devflow.agents.run", run)
    start(project, 184)
    assert AgentMode.READ_ONLY not in modes
    assert AgentMode.EDIT in modes
    tf = read(_active(project))
    assert "Plan" not in body_sections(tf)


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | 0o111)


def test_run_verify_with_spaces_and_backslashes_in_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "dir with spaces" / "worktree"
    _write_exec(
        worktree / "scripts" / "verify",
        "#!/usr/bin/env bash\necho verify-ok\n",
    )
    recorded: list[tuple[object, dict[str, object]]] = []
    real = subprocess.run

    def wrapped(*args: object, **kwargs: object) -> object:
        recorded.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr("devflow.runner.subprocess.run", wrapped)
    passed, output = run_verify(worktree)
    assert passed is True
    assert "verify-ok" in output
    assert recorded
    args, kwargs = recorded[0]
    argv = args[0] if args else kwargs["args"]
    assert isinstance(argv, list)
    assert kwargs.get("cwd") == worktree
    assert kwargs.get("shell") is False
    assert all(str(worktree) not in str(part) for part in argv)
    assert argv[-1] in {"./scripts/verify", "scripts/verify"}
    # Relative script path stays POSIX; Windows Git Bash absolute path may use `\`.
    assert "\\" not in str(argv[-1])
    if os.name == "nt":
        assert argv[0] != "bash"
        assert Path(argv[0]).is_absolute()
    else:
        assert all("\\" not in str(part) for part in argv)


def test_run_verify_streams_and_preserves_final_output(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    _write_exec(
        worktree / "scripts" / "verify",
        ("#!/usr/bin/env bash\necho 00-preflight\nsleep 0.2\necho 40-test\n"),
    )
    activity: list[str] = []
    passed, output = run_verify(worktree, activity.append)
    assert passed is True
    assert activity == ["00-preflight", "40-test"]
    assert output == "00-preflight\n40-test\n"


def test_windows_worktree_argv_uses_resolved_git_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only argv contract")
    git_bash = tmp_path / "Git" / "usr" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_text("", encoding="utf-8")
    wsl = tmp_path / "System32" / "bash.exe"
    wsl.parent.mkdir(parents=True)
    wsl.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name == "bash":
            return str(wsl)
        return None

    monkeypatch.setattr("devflow.runner.shutil.which", fake_which)
    monkeypatch.setenv("PATH", str(wsl.parent))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    argv = _worktree_script_argv("scripts/setup-worktree")
    assert argv[0] != "bash"
    assert Path(argv[0]).resolve() == git_bash.resolve()
    assert argv[1] == "scripts/setup-worktree"


def test_windows_worktree_argv_rejects_wsl_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only argv contract")
    wsl = tmp_path / "System32" / "bash.exe"
    wsl.parent.mkdir(parents=True)
    wsl.write_text("", encoding="utf-8")

    monkeypatch.setattr("devflow.runner.shutil.which", lambda _name: str(wsl))
    monkeypatch.setenv("PATH", str(wsl.parent))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "NoGit"))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(RunnerError, match="Git Bash not found"):
        _worktree_script_argv("scripts/verify")


def test_windows_worktree_scripts_share_git_bash_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only argv contract")
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_text("", encoding="utf-8")
    monkeypatch.setattr("devflow.runner.shutil.which", lambda _name: None)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    setup_argv = _worktree_script_argv("scripts/setup-worktree")
    verify_argv = _worktree_script_argv("scripts/verify")
    assert setup_argv[0] == verify_argv[0] == str(git_bash.resolve())
    assert setup_argv[1] == "scripts/setup-worktree"
    assert verify_argv[1] == "scripts/verify"


def test_non_windows_worktree_argv_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devflow.runner.os.name", "posix")
    argv = _worktree_script_argv("scripts/verify")
    assert argv == ["./scripts/verify"]


def test_run_setup_worktree_propagates_missing_git_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only missing-bash path")
    _write_exec(
        tmp_path / "scripts" / "setup-worktree",
        "#!/usr/bin/env bash\necho ok\n",
    )

    def boom() -> str:
        raise RunnerError("Git Bash not found")

    monkeypatch.setattr("devflow.runner.resolve_git_bash", boom)
    called = False

    def fake_run(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr("devflow.runner.subprocess.run", fake_run)
    with pytest.raises(RunnerError, match="Git Bash not found"):
        run_setup_worktree(tmp_path)
    assert called is False


def test_resolve_git_bash_smoke_on_this_host() -> None:
    if os.name != "nt":
        pytest.skip("Windows host smoke")
    path = Path(resolve_git_bash())
    assert path.is_file()
    key = str(path).replace("/", "\\").casefold()
    assert "bash.exe" in key
    assert "system32" not in key
    assert "windowsapps" not in key
    assert "\\git\\" in key


def test_run_setup_worktree_runs_when_present(tmp_path: Path) -> None:
    _write_exec(
        tmp_path / "scripts" / "setup-worktree",
        "#!/usr/bin/env bash\nprintf 'ok' > marker.txt\n",
    )
    run_setup_worktree(tmp_path)
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "ok"


def test_run_setup_worktree_missing_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr("devflow.runner.subprocess.run", boom)
    run_setup_worktree(tmp_path)


def test_run_setup_worktree_failure_raises(tmp_path: Path) -> None:
    _write_exec(
        tmp_path / "scripts" / "setup-worktree",
        "#!/usr/bin/env bash\necho setup-boom >&2\nexit 1\n",
    )
    with pytest.raises(WorktreeSetupError, match="setup-boom"):
        run_setup_worktree(tmp_path)


def test_setup_worktree_failure_blocks(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git("config", "core.autocrlf", "false", cwd=project)
    _write_exec(
        project / "scripts" / "setup-worktree",
        "#!/usr/bin/env bash\necho setup-boom >&2\nexit 1\n",
    )
    git("add", "-A", cwd=project)
    git("commit", "-m", "add setup-worktree", cwd=project)
    _origin_main(project)
    path = _task(
        project,
        risk_proposed=Risk.LOW,
        complexity_proposed=Complexity.LOW,
        modules=["src/app.py"],
    )
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    with pytest.raises(RunnerError, match="setup-boom"):
        start(project, 184)
    assert not task_worktree(project, 184).exists()
    assert read(path).frontmatter.state == "BACKLOG"


def test_verify_failure_prints_last_20_lines(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [f"verify-row-{index:02d}" for index in range(1, 26)]
    output = "\n".join(rows) + "\n"
    path = _task(project, **_epic())
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    monkeypatch.setattr("devflow.runner.run_verify", lambda _wt: (False, output))
    result = start(project, 184)
    joined = "\n".join(result.messages)
    assert "--- verify output (last 20 lines) ---" in joined
    for row in rows[:5]:
        assert row not in joined
    for row in rows[-20:]:
        assert row in joined
    tf = read(_active(project))
    for row in rows:
        assert row in tf.body


def _review_status_agent(
    task_file_path: Path, review_status: AgentStatus, detail: str | None = None
):
    base = _ok_agent(task_file_path)

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        if mode is AgentMode.REVIEW:
            return AgentResult(review_status, "", detail, 0.2)
        return base(agent, prompt_file, worktree, mode, **kwargs)

    return run


def test_blocked_reviewer_fail_closed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    monkeypatch.setattr(
        "devflow.agents.run",
        _review_status_agent(path, AgentStatus.BLOCKED, "quota unavailable"),
    )
    result = start(project, 184)
    assert result.final_state is State.BLOCKED
    assert result.review_record is None
    tf = read(_active(project))
    assert tf.frontmatter.blocked_reason == "REVIEWER_UNAVAILABLE"
    assert tf.frontmatter.review_records == []
    assert "READY_TO_MERGE" not in tf.body
    assert "## Review error" in tf.body
    assert "status: blocked" in tf.body
    assert "quota unavailable" in tf.body
    assert not (project / ".devflow" / "worktrees" / "review-184-r1").exists()
    branches = git("branch", "--list", "review/184-r1", cwd=project).stdout
    assert branches.strip() == ""
    seen = [
        item
        for item in result.messages
        if "reviewer" in item or "Codex" in item or "codex" in item
    ]
    assert any("reviewer (claude, review)" in item for item in result.messages)
    assert not any("codex" in item.casefold() for item in seen)


def test_retry_reviewer_fail_closed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    monkeypatch.setattr(
        "devflow.agents.run",
        _review_status_agent(path, AgentStatus.RETRY, "usage limit"),
    )
    result = start(project, 184)
    assert result.final_state is State.BLOCKED
    assert result.review_record is None
    tf = read(_active(project))
    assert tf.frontmatter.blocked_reason == "REVIEWER_UNAVAILABLE"
    assert tf.frontmatter.review_records == []
    assert not any("merge readiness: open" in item for item in result.messages)


def test_successful_reviewer_clean_path(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, "[]"))
    result = start(project, 184)
    assert result.final_state is State.READY_TO_MERGE
    assert result.review_record is not None
    assert result.review_record.blocking_findings == 0
    assert any("merge readiness: open" in item for item in result.messages)
    assert any("merge gate... OK" in item for item in result.messages)


def test_ready_to_merge_commits_final_journal(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devflow.freshness import check_code_freshness

    path = _task(project, **_epic())
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, "[]"))
    result = start(project, 184)
    assert result.final_state is State.READY_TO_MERGE
    assert result.worktree is not None
    wt = result.worktree
    porcelain = git("status", "--porcelain", cwd=wt).stdout.strip()
    assert porcelain == ""
    head = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    journal = git("show", f"{head}:.devflow/tasks/184.md", cwd=wt).stdout
    assert "state: READY_TO_MERGE" in journal
    assert "review_records:" in journal
    assert "review_clean" in journal
    assert "READY_TO_MERGE" in journal
    record = result.review_record
    assert record is not None
    assert record.head_sha != head
    base = git("rev-parse", "origin/main", cwd=wt).stdout.strip()
    freshness = check_code_freshness(wt, record, head, base)
    assert freshness.fresh is True
    msg = git("log", "-1", "--format=%s", cwd=wt).stdout.strip()
    assert msg == "Finalize task 184 journal"


def test_merge_readiness_lists_blockers(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    findings = "```yaml\nid: F1\nseverity: HIGH\nproblem: maybe\n```\n"
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, findings))
    result = start(project, 184)
    joined = "\n".join(result.messages)
    assert "merge readiness: blocked (" in joined
    assert "  - " in joined
    assert any(item.strip().startswith("184:   - ") for item in result.messages)


def _seed_resume_worktree(project: Path, *, state: str) -> Path:
    main_path = _task(project, state=state, **_epic())
    wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
    dest = wt / ".devflow" / "tasks" / "184.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(main_path, dest)
    (wt / "src" / "app.py").write_text("print('implemented')\n", encoding="utf-8")
    body = read(dest).body
    if "Doc impact" not in body:
        append_section(dest, "Doc impact", "status: none\nfiles: []\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "Implement task 184", cwd=wt)
    return dest


def _counting_agent(task_file_path: Path, review_yaml: str = "[]"):
    counts = {"edit": 0, "review": 0}
    base = _ok_agent(task_file_path, review_yaml)

    def run(
        agent: str,
        prompt_file: Path,
        worktree: Path,
        mode: AgentMode,
        **kwargs: object,
    ) -> AgentResult:
        if mode is AgentMode.EDIT:
            counts["edit"] += 1
        if mode is AgentMode.REVIEW:
            counts["review"] += 1
        return base(agent, prompt_file, worktree, mode, **kwargs)

    return run, counts


def test_resume_implementing_skips_implementer(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_resume_worktree(project, state="IMPLEMENTING")
    impl_sha = git("rev-parse", "HEAD", cwd=task_worktree(project, 184)).stdout.strip()
    agent, counts = _counting_agent(path, "[]")
    monkeypatch.setattr("devflow.agents.run", agent)
    rebase_calls: list[str] = []

    def wrap_rebase(worktree: Path, base: str) -> str:
        rebase_calls.append(base)
        return rebase_onto_base(worktree, base)

    monkeypatch.setattr("devflow.gitops.rebase_onto_base", wrap_rebase)
    result = resume(project, 184)
    assert counts["edit"] == 0
    assert counts["review"] == 1
    assert rebase_calls == ["origin/main"]
    assert any("merge gate... OK" in item for item in result.messages)
    assert any("IMPLEMENTING -> REVIEW" in item for item in result.messages)
    assert result.final_state is State.READY_TO_MERGE
    assert result.worktree is not None
    assert git("status", "--porcelain", cwd=result.worktree).stdout.strip() == ""
    head = git("rev-parse", "HEAD", cwd=result.worktree).stdout.strip()
    journal = git("show", f"{head}:.devflow/tasks/184.md", cwd=result.worktree).stdout
    assert "state: READY_TO_MERGE" in journal
    log = git("log", "--format=%H", cwd=result.worktree).stdout
    assert impl_sha in log


def test_resume_rework_transitions_to_review(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_resume_worktree(project, state="REWORK")
    wt = task_worktree(project, 184)
    (wt / "src" / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    agent, counts = _counting_agent(path, "[]")
    monkeypatch.setattr("devflow.agents.run", agent)
    result = resume(project, 184)
    assert counts["edit"] == 0
    assert counts["review"] == 1
    assert any("REWORK -> REVIEW" in item for item in result.messages)
    assert result.final_state is State.READY_TO_MERGE
    assert not any("rerun devflow start" in item for item in result.messages)


def test_resume_review_retries_reviewer(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_resume_worktree(project, state="REVIEW")
    agent, counts = _counting_agent(path, "[]")
    monkeypatch.setattr("devflow.agents.run", agent)
    result = resume(project, 184)
    assert counts["edit"] == 0
    assert counts["review"] == 1
    assert result.final_state is State.READY_TO_MERGE
    assert result.review_record is not None


def test_resume_wrong_states_fail_closed(project: Path) -> None:
    cases = [
        ("BACKLOG", "use: devflow start 184"),
        ("PLAN_APPROVAL", "use: devflow approve 184"),
        ("BLOCKED", "resolve the block first"),
        ("READY_TO_MERGE", "already ready for merge"),
        ("MERGED", "already merged"),
        ("CANCELLED", "cancelled"),
    ]
    main_path = project / ".devflow" / "tasks" / "184.md"
    for state, needle in cases:
        if main_path.is_file():
            main_path.unlink()
        create(main_path, 184, "Order cancel", state=state, **_epic())  # type: ignore[arg-type]
        wt = ensure_task_worktree(project, 184, "Order cancel", "origin/main")
        dest = wt / ".devflow" / "tasks" / "184.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(main_path, dest)
        with pytest.raises(RunnerError, match=needle):
            resume(project, 184)
        remove_task_worktree(project, 184)
        branch = git(
            "branch", "--list", "task/184-order-cancel", cwd=project
        ).stdout.strip()
        if branch:
            git("branch", "-D", "task/184-order-cancel", cwd=project)


def test_resume_missing_worktree_fails(project: Path) -> None:
    _task(project, state="IMPLEMENTING", **_epic())
    with pytest.raises(RunnerError, match="RESUME_CONTEXT_MISSING"):
        resume(project, 184)


def test_start_still_backlog_only_after_resume(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_resume_worktree(project, state="IMPLEMENTING")
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path))
    with pytest.raises(RunnerError, match="expected BACKLOG"):
        start(project, 184)


def test_blocking_review_points_to_resume(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _task(project, **_epic())
    findings = "```yaml\nid: F1\nseverity: HIGH\nevidence: test\nproblem: bug\n```\n"
    monkeypatch.setattr("devflow.agents.run", _ok_agent(path, findings))
    result = start(project, 184)
    assert result.final_state is State.REWORK
    assert any("run: devflow resume 184" in item for item in result.messages)
    assert not any("rerun devflow start" in item for item in result.messages)
