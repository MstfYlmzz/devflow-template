from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from devflow.agents import AgentMode, AgentResult, AgentStatus
from devflow.gitops import ensure_task_worktree, task_worktree
from devflow.issues import fetch_issue
from devflow.paths import resolve_task, task_path
from devflow.policy import Complexity, Risk
from devflow.runner import RunnerError, approve, cancel, start, stop
from devflow.states import State
from devflow.taskfile import append_section, create, read
from tests.conftest import git

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "project"
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
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    _origin_main(repo)


@pytest.fixture
def project(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed(git_repo)
    monkeypatch.setattr("devflow.runner.run_verify", lambda _wt: (True, "ok\n"))
    return git_repo


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, object] | None = None,
    stderr: str = "",
    returncode: int = 0,
    missing: bool = False,
) -> None:
    real_run = subprocess.run

    def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if not argv or argv[0] != "gh":
            return real_run(argv, **kwargs)
        if missing:
            raise FileNotFoundError("gh")
        assert argv[:3] == ["gh", "issue", "view"]
        stdout = json.dumps(payload or {}) if returncode == 0 else ""
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(subprocess, "run", fake_run)


def _ok_agent():
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


def test_fetch_issue_parses_open_issue(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gh(
        monkeypatch,
        payload={
            "number": 27,
            "title": "Add cancellation guard",
            "body": "Prevent duplicate cancellation.",
            "state": "OPEN",
        },
    )
    issue = fetch_issue(git_repo, 27)
    assert issue.number == 27
    assert issue.title == "Add cancellation guard"
    assert "duplicate" in issue.body
    assert issue.state == "OPEN"


def test_new_issue_materializes_in_worktree_only(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gh(
        monkeypatch,
        payload={
            "number": 27,
            "title": "Add cancellation guard",
            "body": "Prevent duplicate cancellation.",
            "state": "OPEN",
        },
    )
    monkeypatch.setattr("devflow.agents.run", _ok_agent())
    result = start(project, 27, risk_hint=Risk.HIGH)
    assert result.worktree is not None
    wt_file = task_path(result.worktree, 27)
    assert wt_file.is_file()
    assert "Prevent duplicate cancellation." in wt_file.read_text(encoding="utf-8")
    assert not task_path(project, 27).exists()
    branch = git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=result.worktree
    ).stdout.strip()
    assert branch.startswith("task/27-")
    assert result.final_state is State.PLAN_APPROVAL


def test_main_checkout_stays_clean_after_issue_start(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gh(
        monkeypatch,
        payload={
            "number": 27,
            "title": "Add cancellation guard",
            "body": "body",
            "state": "OPEN",
        },
    )
    monkeypatch.setattr("devflow.agents.run", _ok_agent())
    before = git("status", "--porcelain", "-uno", cwd=project).stdout
    start(project, 27, risk_hint=Risk.HIGH)
    after = git("status", "--porcelain", "-uno", cwd=project).stdout
    assert after == before
    assert not task_path(project, 27).exists()
    porcelain = git("status", "--porcelain", cwd=project).stdout
    assert "tasks/27.md" not in porcelain
    assert ".devflow/tasks/27.md" not in porcelain.replace("\\", "/")


def test_active_worktree_wins_over_main(project: Path) -> None:
    create(task_path(project, 27), 27, "Main copy", state="BACKLOG")
    wt = ensure_task_worktree(project, 27, "Worktree copy", "origin/main")
    create(task_path(wt, 27), 27, "Worktree copy", state="PLAN_APPROVAL")
    found = resolve_task(project, 27)
    assert found == task_path(wt, 27)
    assert read(found).frontmatter.state == "PLAN_APPROVAL"


def test_approve_finds_worktree_only_task(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = ensure_task_worktree(project, 27, "Add cancellation guard", "origin/main")
    path = task_path(wt, 27)
    create(
        path,
        27,
        "Add cancellation guard",
        state="PLAN_APPROVAL",
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    append_section(path, "Plan", "- do X\n")
    assert not task_path(project, 27).exists()

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
            append_section(
                task_path(worktree, 27), "Doc impact", "status: none\nfiles: []\n"
            )
        return AgentResult(AgentStatus.OK, "ok", None, 0.5)

    monkeypatch.setattr("devflow.agents.run", run)
    result = approve(project, 27, skip_review=True, reason="ship it")
    assert result.final_state is not State.PLAN_APPROVAL
    assert result.worktree == wt
    assert not task_path(project, 27).exists()


def test_stop_mutates_worktree_not_main(project: Path) -> None:
    wt = ensure_task_worktree(project, 27, "t", "origin/main")
    create(task_path(wt, 27), 27, "t", state="IMPLEMENTING")
    assert not task_path(project, 27).exists()
    stop(project, 27)
    assert read(task_path(wt, 27)).frontmatter.state == "BLOCKED"
    assert not task_path(project, 27).exists()


def test_cancel_mutates_worktree_not_main(project: Path) -> None:
    wt = ensure_task_worktree(project, 27, "t", "origin/main")
    create(task_path(wt, 27), 27, "t", state="IMPLEMENTING")
    cancel(project, 27, reason="drop")
    assert read(task_path(wt, 27)).frontmatter.state == "CANCELLED"
    assert not task_path(project, 27).exists()


def test_legacy_main_task_promotes_to_worktree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_path = task_path(project, 184)
    create(
        main_path,
        184,
        "Order cancel",
        risk_proposed=Risk.LOW,
        complexity_proposed=Complexity.LOW,
        modules=["src/app.py"],
    )

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
            append_section(
                task_path(worktree, 184), "Doc impact", "status: none\nfiles: []\n"
            )
        return AgentResult(AgentStatus.OK, "ok", None, 0.5)

    monkeypatch.setattr("devflow.agents.run", run)
    result = start(project, 184, skip_review=True, reason="legacy")
    assert result.worktree is not None
    active = task_path(result.worktree, 184)
    assert active.is_file()
    assert read(active).frontmatter.state != "BACKLOG"
    assert read(main_path).frontmatter.state == "BACKLOG"


def test_issue_not_found_leaves_repo_clean(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gh(
        monkeypatch,
        returncode=1,
        stderr="could not resolve to an Issue with the number of 99",
    )
    with pytest.raises(RunnerError, match="issue 99 not found"):
        start(project, 99)
    assert not task_worktree(project, 99).exists()
    assert not task_path(project, 99).exists()
    assert git("status", "--porcelain", cwd=project).stdout == ""


def test_closed_issue_rejected(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh(
        monkeypatch,
        payload={"number": 27, "title": "x", "body": "y", "state": "CLOSED"},
    )
    with pytest.raises(RunnerError, match="issue 27 is closed"):
        start(project, 27)
    assert not task_worktree(project, 27).exists()
    assert not task_path(project, 27).exists()


def test_gh_unavailable(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh(monkeypatch, missing=True)
    with pytest.raises(RunnerError, match="GitHub CLI not found"):
        start(project, 27)
    assert not task_worktree(project, 27).exists()


def test_reattach_existing_branch_without_new_branch(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = ensure_task_worktree(project, 27, "Add cancellation guard", "origin/main")
    path = task_path(wt, 27)
    create(
        path,
        27,
        "Add cancellation guard",
        state="PLAN_APPROVAL",
        risk_proposed=Risk.HIGH,
        complexity_proposed=Complexity.MEDIUM,
        modules=["src/app.py"],
    )
    append_section(path, "Plan", "- do X\n")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip()
    git("add", "-A", cwd=wt)
    git("commit", "-m", "persist task file", cwd=wt)
    git("worktree", "remove", "--force", str(wt), cwd=project)
    assert not task_worktree(project, 27).exists()

    monkeypatch.setattr(
        "devflow.agents.run",
        lambda *a, **k: AgentResult(AgentStatus.BLOCKED, "", "stop", 0.1),
    )
    result = approve(project, 27, skip_review=True, reason="ship")
    assert result.worktree is not None
    assert result.worktree.is_dir()
    assert task_path(result.worktree, 27).is_file()
    current = git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=result.worktree
    ).stdout.strip()
    assert current == branch


def test_issue_dry_run_does_not_mutate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gh(
        monkeypatch,
        payload={
            "number": 27,
            "title": "Add cancellation guard",
            "body": "body",
            "state": "OPEN",
        },
    )
    before = git("status", "--porcelain", cwd=project).stdout
    branches_before = git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=project
    ).stdout
    result = start(project, 27, dry_run=True)
    assert result.final_state is State.BACKLOG
    assert result.worktree is None
    assert not task_worktree(project, 27).exists()
    assert not task_path(project, 27).exists()
    assert git("status", "--porcelain", cwd=project).stdout == before
    assert (
        git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=project
        ).stdout
        == branches_before
    )
    assert any("dry-run" in item for item in result.messages)
