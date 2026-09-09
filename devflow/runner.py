"""Orchestrate `devflow start`. Binds existing modules; invents no new rules."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from devflow import agents as agents_api
from devflow import gitops
from devflow.agents import (
    COMMANDS,
    AgentMode,
    AgentResult,
    AgentStatus,
    _resolve_command,
    terminate_tree,
)
from devflow.authority import (
    check_agent_output_for_violations,
    load_policy_from_base,
    sanitized_env,
)
from devflow.capabilities import ProviderCapabilities, discover_provider
from devflow.ci_checks import changed_files
from devflow.freshness import ReviewRecord
from devflow.issues import GitHubIssue, fetch_issue
from devflow.lock import (
    LockHeld,
    StaleLock,
    acquire,
    read_lock,
    release,
    set_agent_pid,
)
from devflow.paths import path_in_worktree, resolve_task, task_path
from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    Risk,
    RoutingDecision,
    TriageSignals,
    check_merge_gate,
    decide,
    needed_triage_fields,
    review_provider,
    triage_provider,
)
from devflow.prompts import build_prompt
from devflow.review import parse_findings
from devflow.runtime import (
    RUNTIME_ROLES,
    RuntimeChoice,
    RuntimeSelection,
    recommended_effort,
)
from devflow.states import (
    State,
    Trigger,
    check_ready_to_merge,
    initial_state,
    review_cycle_count,
    transition,
)
from devflow.taskfile import (
    TaskFile,
    TaskFrontmatter,
    append_section,
    body_sections,
    create,
    decision_inputs,
    estimate_paths,
    format_estimated_floor_matches,
    read,
    update_frontmatter,
)

BASE_REF = "origin/main"
_FENCE_RE = re.compile(r"```(?:yaml)?\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
RuntimeSelector = Callable[
    [
        RoutingDecision,
        dict[str, str],
        RuntimeSelection,
        dict[str, ProviderCapabilities],
    ],
    RuntimeSelection,
]


class RunnerError(Exception):
    """A start/stop/cancel precondition failed."""


class WorktreeSetupError(Exception):
    """scripts/setup-worktree failed after the worktree was created."""


@dataclass
class StartResult:
    task_id: int
    final_state: State
    worktree: Path | None
    verify_passed: bool | None
    review_record: ReviewRecord | None
    messages: list[str] = field(default_factory=list)


def start(
    repo: Path,
    task_id: int,
    risk_hint: Risk | None = None,
    skip_review: bool = False,
    review_advisory: bool = False,
    reason: str | None = None,
    dry_run: bool = False,
    after_approval: bool = False,
    runtime_selector: RuntimeSelector | None = None,
) -> StartResult:
    if dry_run:
        return _dry_run_entry(
            repo,
            task_id,
            risk_hint,
            skip_review,
            review_advisory,
            reason,
            after_approval,
        )

    acquired = False
    try:
        try:
            acquire(task_id, "start", repo)
            acquired = True
        except LockHeld as exc:
            resolved = resolve_task(repo, task_id, attach=False)
            state = (
                State(read(resolved).frontmatter.state)
                if resolved is not None
                else State.BACKLOG
            )
            return StartResult(task_id, state, None, None, None, [str(exc)])
        except StaleLock as exc:
            resolved = resolve_task(repo, task_id, attach=False)
            state = (
                State(read(resolved).frontmatter.state)
                if resolved is not None
                else State.BACKLOG
            )
            return StartResult(
                task_id,
                state,
                None,
                None,
                None,
                [str(exc), f"run: devflow recover {task_id}"],
            )

        messages: list[str] = []
        try:
            path, tf = _prepare_task_context(
                repo, task_id, messages, after_approval=after_approval
            )
        except WorktreeSetupError as exc:
            raise RunnerError(f"setup-worktree failed: {exc}") from exc
        current = State(tf.frontmatter.state)
        return _run_locked(
            repo,
            path,
            tf,
            current,
            risk_hint,
            skip_review,
            review_advisory,
            reason,
            after_approval,
            messages,
            runtime_selector,
        )
    finally:
        if acquired:
            release(task_id, repo)


def approve(
    repo: Path,
    task_id: int,
    risk_hint: Risk | None = None,
    skip_review: bool = False,
    review_advisory: bool = False,
    reason: str | None = None,
) -> StartResult:
    return start(
        repo,
        task_id,
        risk_hint=risk_hint,
        skip_review=skip_review,
        review_advisory=review_advisory,
        reason=reason,
        after_approval=True,
    )


_RESUME_STATES = frozenset({State.IMPLEMENTING, State.REWORK, State.REVIEW})


def resume(
    repo: Path,
    task_id: int,
    risk_hint: Risk | None = None,
    skip_review: bool = False,
    review_advisory: bool = False,
    reason: str | None = None,
) -> StartResult:
    """Continue IMPLEMENTING / REWORK / REVIEW without re-running implementer."""
    acquired = False
    try:
        try:
            acquire(task_id, "resume", repo)
            acquired = True
        except LockHeld as exc:
            resolved = resolve_task(repo, task_id, attach=False)
            state = (
                State(read(resolved).frontmatter.state)
                if resolved is not None
                else State.BACKLOG
            )
            return StartResult(task_id, state, None, None, None, [str(exc)])
        except StaleLock as exc:
            resolved = resolve_task(repo, task_id, attach=False)
            state = (
                State(read(resolved).frontmatter.state)
                if resolved is not None
                else State.BACKLOG
            )
            return StartResult(
                task_id,
                state,
                None,
                None,
                None,
                [str(exc), f"run: devflow recover {task_id}"],
            )

        messages: list[str] = []
        path, tf, worktree = _prepare_resume_context(repo, task_id, messages)
        current = State(tf.frontmatter.state)
        if current not in _RESUME_STATES:
            raise RunnerError(_resume_state_error(task_id, current))

        blockers = _unmerged_blockers(repo, tf)
        if blockers:
            listed = ", ".join(str(item) for item in blockers)
            raise RunnerError(f"blocked by unmerged tasks: {listed}")

        policy = load_policy_from_base(repo, BASE_REF)
        _emit(messages, task_id, f"policy from {BASE_REF}")
        decision = _decide(tf, policy, risk_hint)
        _emit(
            messages,
            task_id,
            (
                f"risk {decision.risk.value}, complexity {decision.complexity.value}"
                f" -> {decision.implementer}, plan {decision.plan_detail}"
            ),
        )
        _require_bypass_reason(decision, skip_review, review_advisory, reason)
        tf = _ensure_runtime_selection(path, tf, decision, policy, messages, None)

        if current is State.REVIEW:
            return _resume_review(
                repo,
                path,
                tf,
                decision,
                policy,
                skip_review,
                review_advisory,
                messages,
                worktree,
            )
        commit_message = (
            f"Implement task {task_id}"
            if current is State.IMPLEMENTING
            else f"Rework task {task_id}"
        )
        return _post_change_pipeline(
            repo,
            path,
            tf,
            decision,
            policy,
            skip_review,
            review_advisory,
            messages,
            worktree,
            from_state=current,
            commit_message=commit_message,
        )
    finally:
        if acquired:
            release(task_id, repo)


def stop(repo: Path, task_id: int) -> StartResult:
    path = resolve_task(repo, task_id)
    if path is None:
        raise RunnerError(f"task file not found: {task_id}")
    tf = read(path)
    current = State(tf.frontmatter.state)
    if current is State.MERGED:
        raise RunnerError(f"task {task_id} is MERGED")
    _stop_running_agent(task_id, repo)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    update_frontmatter(
        path,
        state=State.BLOCKED.value,
        blocked_from=current.value,
        blocked_reason="USER_STOPPED",
    )
    append_section(path, "Stop", f"stopped {now} from {current.value}")
    messages = [f"{task_id}: {current.value} -> BLOCKED (USER_STOPPED)"]
    print(messages[0], flush=True)
    worktree = gitops.task_worktree(repo, task_id)
    return StartResult(
        task_id,
        State.BLOCKED,
        worktree if worktree.is_dir() else None,
        None,
        None,
        messages,
    )


def cancel(repo: Path, task_id: int, reason: str, discard: bool = False) -> StartResult:
    if not reason or not str(reason).strip():
        raise RunnerError("--reason is required")
    path = resolve_task(repo, task_id)
    if path is None:
        raise RunnerError(f"task file not found: {task_id}")
    tf = read(path)
    current = State(tf.frontmatter.state)
    if current is State.MERGED:
        raise RunnerError(f"task {task_id} is MERGED")
    _stop_running_agent(task_id, repo)
    update_frontmatter(path, state=State.CANCELLED.value)
    append_section(path, "Decisions", reason.strip())
    worktree = gitops.task_worktree(repo, task_id)
    kept = worktree if worktree.is_dir() else None
    if discard:
        branch = gitops.task_branch_name(repo, task_id)
        gitops.remove_task_worktree(repo, task_id)
        if branch:
            gitops.delete_local_branch(repo, branch)
        kept = None
    messages = [f"{task_id}: {current.value} -> CANCELLED"]
    print(messages[0], flush=True)
    return StartResult(task_id, State.CANCELLED, kept, None, None, messages)


def _is_wsl_bash(path: Path) -> bool:
    """Return True for WSL / WindowsApps bash launchers (not Git Bash)."""
    key = str(path).replace("/", "\\").casefold()
    markers = (
        "\\system32\\bash.exe",
        "\\system32\\bash",
        "\\windowsapps\\",
        "\\wsl\\",
        "wsl.exe",
    )
    return any(marker in key for marker in markers)


def _is_git_for_windows_bash(path: Path) -> bool:
    """Heuristic: Git for Windows installs under a ``Git`` directory."""
    key = str(path).replace("/", "\\").casefold()
    if _is_wsl_bash(path):
        return False
    return "\\git\\" in key and key.endswith("bash.exe")


def resolve_git_bash() -> str:
    """Resolve Git for Windows ``bash.exe`` to an absolute path.

    Bare ``\"bash\"`` under Windows ``CreateProcess`` often launches WSL's
    ``bash.exe`` instead of Git Bash. Worktree scripts must use Git Bash.
    """
    if os.name != "nt":
        bash = shutil.which("bash")
        if not bash:
            raise RunnerError("Git Bash not found")
        return str(Path(bash).resolve())

    candidates: list[Path] = []
    which = shutil.which("bash")
    if which:
        candidates.append(Path(which))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.strip():
            continue
        candidates.append(Path(entry) / "bash.exe")
        candidates.append(Path(entry) / "bash")
    for base_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(base_key, "").strip()
        if not base:
            continue
        root = Path(base) / "Git"
        candidates.append(root / "bin" / "bash.exe")
        candidates.append(root / "usr" / "bin" / "bash.exe")

    seen: set[str] = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if not resolved.is_file():
            continue
        if _is_wsl_bash(resolved):
            continue
        if _is_git_for_windows_bash(resolved):
            return str(resolved)

    raise RunnerError("Git Bash not found")


def _worktree_script_argv(relative: str) -> list[str]:
    rel = relative.replace("\\", "/")
    if os.name == "nt":
        return [resolve_git_bash(), rel]
    return [f"./{rel}"]


def _run_worktree_script(
    worktree: Path, relative: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _worktree_script_argv(relative),
        cwd=worktree,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )


def run_verify(
    worktree: Path, on_output: Callable[[str], None] | None = None
) -> tuple[bool, str]:
    script = worktree / "scripts" / "verify"
    if not script.is_file():
        return False, "scripts/verify not found"
    if on_output is None and sys.stdin.isatty() and sys.stdout.isatty():

        def emit_verify_line(line: str) -> None:
            print(f"  verify: {line}", flush=True)

        on_output = emit_verify_line
    if on_output is not None:
        argv = _worktree_script_argv("scripts/verify")
        kwargs: dict[str, object] = {
            "cwd": worktree,
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)  # type: ignore[call-overload]
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                on_output(line.rstrip())
            code = proc.wait()
        except KeyboardInterrupt:
            terminate_tree(proc.pid)
            proc.wait(timeout=1)
            raise
        return code == 0, "".join(lines)
    result = _run_worktree_script(worktree, "scripts/verify")
    return result.returncode == 0, f"{result.stdout}{result.stderr}"


def run_setup_worktree(worktree: Path) -> None:
    script = worktree / "scripts" / "setup-worktree"
    if not script.is_file():
        return
    result = _run_worktree_script(worktree, "scripts/setup-worktree")
    if result.returncode != 0:
        output = f"{result.stdout}{result.stderr}".strip() or "setup-worktree failed"
        raise WorktreeSetupError(output)


def _prepare_task_context(
    repo: Path,
    task_id: int,
    messages: list[str],
    *,
    after_approval: bool,
) -> tuple[Path, TaskFile]:
    """Materialize or promote the task file into the worktree before mutation."""
    resolved = resolve_task(repo, task_id, attach=True)
    if resolved is None:
        if after_approval:
            raise RunnerError(f"task file not found: {task_id}")
        issue = _load_issue(repo, task_id)
        worktree = _ensure_worktree(repo, task_id, issue.title, messages)
        path = _materialize_issue(worktree, issue)
        _emit(messages, task_id, f"materialized from issue #{issue.number}")
        return path, read(path)

    title = read(resolved).frontmatter.title
    worktree = _ensure_worktree(repo, task_id, title, messages)
    path = task_path(worktree, task_id)
    if not path_in_worktree(repo, task_id, resolved):
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, path)
            _emit(messages, task_id, "copied task file into worktree")
    if not path.is_file():
        raise RunnerError(f"task file not found in worktree: {path}")
    return path, read(path)


def _load_issue(repo: Path, task_id: int) -> GitHubIssue:
    try:
        return fetch_issue(repo, task_id)
    except RuntimeError as exc:
        raise RunnerError(str(exc)) from exc


def _materialize_issue(worktree: Path, issue: GitHubIssue) -> Path:
    path = task_path(worktree, issue.number)
    if path.is_file():
        return path
    body = issue.body.strip() if issue.body.strip() else "(no description)"
    create(
        path,
        issue.number,
        issue.title,
        body=(f"# Task {issue.number} — {issue.title}\n\n## Issue\n\n{body}\n"),
    )
    return path


def _ephemeral_task_from_issue(issue: GitHubIssue) -> TaskFile:
    body = issue.body.strip() if issue.body.strip() else "(no description)"
    return TaskFile(
        frontmatter=TaskFrontmatter(
            id=issue.number,
            title=issue.title,
            epic=None,
            state="BACKLOG",
            risk_proposed=None,
            risk_reason=None,
            complexity_proposed=None,
            modules=[],
            blocked_by=[],
            adr=[],
            floor_risk=None,
            floor_matched=[],
            signals=None,
            architecture_impact=None,
            uncertain=False,
            floor_risk_actual=None,
            floor_matched_actual=[],
            blocked_from=None,
            blocked_reason=None,
            review_records=[],
            runtime_selection=None,
        ),
        body=f"# Task {issue.number} — {issue.title}\n\n## Issue\n\n{body}\n",
        path=Path(f"<issue:{issue.number}>"),
    )


def _dry_run_entry(
    repo: Path,
    task_id: int,
    risk_hint: Risk | None,
    skip_review: bool,
    review_advisory: bool,
    reason: str | None,
    after_approval: bool,
) -> StartResult:
    resolved = resolve_task(repo, task_id, attach=False)
    if resolved is None:
        if after_approval:
            raise RunnerError(f"task file not found: {task_id}")
        issue = _load_issue(repo, task_id)
        tf = _ephemeral_task_from_issue(issue)
    else:
        tf = read(resolved)
    return _dry_run(
        repo,
        tf,
        State(tf.frontmatter.state),
        risk_hint,
        skip_review,
        review_advisory,
        reason,
        after_approval,
    )


def _emit_verify_result(
    path: Path,
    messages: list[str],
    task_id: int,
    passed: bool,
    verify_out: str,
) -> None:
    _emit(messages, task_id, f"verify... {'PASS' if passed else 'FAIL'}")
    if not passed:
        _emit(messages, task_id, "--- verify output (last 20 lines) ---")
        for line in verify_out.splitlines()[-20:]:
            _emit(messages, task_id, line)
        _emit(messages, task_id, "---")
    append_section(path, "Verify", verify_out.strip() or "(no output)")


def _run_locked(
    repo: Path,
    path: Path,
    tf: TaskFile,
    current: State,
    risk_hint: Risk | None,
    skip_review: bool,
    review_advisory: bool,
    reason: str | None,
    after_approval: bool,
    messages: list[str],
    runtime_selector: RuntimeSelector | None,
) -> StartResult:
    task_id = tf.frontmatter.id
    if after_approval:
        if current is not State.PLAN_APPROVAL:
            raise RunnerError(
                f"task {task_id} is {current.value}, expected PLAN_APPROVAL"
            )
    elif current is not State.BACKLOG:
        raise RunnerError(f"task {task_id} is {current.value}, expected BACKLOG")

    blockers = _unmerged_blockers(repo, tf)
    if blockers:
        listed = ", ".join(str(item) for item in blockers)
        raise RunnerError(f"blocked by unmerged tasks: {listed}")

    policy = load_policy_from_base(repo, BASE_REF)
    _emit(messages, task_id, f"policy from {BASE_REF}")
    decision = _decide(tf, policy, risk_hint)
    _emit(
        messages,
        task_id,
        (
            f"risk {decision.risk.value}, complexity {decision.complexity.value}"
            f" -> {decision.implementer}, plan {decision.plan_detail}"
        ),
    )
    tf = _record_floor(path, tf, policy)
    _require_bypass_reason(decision, skip_review, review_advisory, reason)

    if after_approval:
        _require_plan(tf)
        tf = _apply(path, current, Trigger.PLAN_APPROVED, decision, messages)
        return _implement_onward(
            repo, path, tf, decision, policy, skip_review, review_advisory, messages
        )

    target = initial_state(decision, _has_epic(tf))

    if target is State.TRIAGE:
        tf = _apply(
            path, current, Trigger.START, decision, messages, has_epic=_has_epic(tf)
        )
        tf, decision = _run_triage(
            repo, path, tf, decision, policy, risk_hint, messages
        )
        if State(tf.frontmatter.state) is State.BLOCKED:
            blocked_wt = gitops.task_worktree(repo, task_id)
            return StartResult(
                task_id,
                State.BLOCKED,
                blocked_wt if blocked_wt.is_dir() else None,
                None,
                None,
                messages,
            )

    tf = _ensure_runtime_selection(
        path, tf, decision, policy, messages, runtime_selector
    )
    worktree: Path | None = gitops.task_worktree(repo, task_id)
    if worktree is not None and not worktree.is_dir():
        worktree = None
    if decision.plan_required:
        planned = _write_plan(repo, path, tf, decision, policy, messages)
        if isinstance(planned, StartResult):
            return planned
        tf, worktree = planned

    current_state = State(tf.frontmatter.state)
    if current_state is State.BACKLOG:
        tf = _apply(
            path,
            current_state,
            Trigger.START,
            decision,
            messages,
            has_epic=_has_epic(tf),
        )
    elif current_state is State.TRIAGE:
        tf = _apply(path, current_state, Trigger.TRIAGE_DONE, decision, messages)

    if State(tf.frontmatter.state) is State.PLAN_APPROVAL:
        _emit(messages, task_id, f"run: devflow approve {task_id}")
        return StartResult(task_id, State.PLAN_APPROVAL, worktree, None, None, messages)

    return _implement_onward(
        repo, path, tf, decision, policy, skip_review, review_advisory, messages
    )


def _implement_onward(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    skip_review: bool,
    review_advisory: bool,
    messages: list[str],
) -> StartResult:
    task_id = tf.frontmatter.id
    title = tf.frontmatter.title
    timeout = _timeout(policy)
    try:
        worktree = _ensure_worktree(repo, task_id, title, messages)
    except WorktreeSetupError as exc:
        return _setup_worktree_blocked(path, tf, decision, messages, exc)

    prompt = build_prompt(
        "implementer",
        tf,
        worktree if (worktree / ".ai" / "roles").is_dir() else repo,
        {"plan_detail": decision.plan_detail},
    )
    result = _run_agent(
        repo,
        task_id,
        decision.implementer,
        prompt,
        worktree,
        AgentMode.EDIT,
        timeout,
        path,
        _runtime_choice(tf, "implementer", decision.implementer, decision),
    )
    _emit_agent_result(
        messages, task_id, "implementer", decision.implementer, AgentMode.EDIT, result
    )
    if result.status is not AgentStatus.OK:
        _record_agent_error(
            path, "Implementer error", decision.implementer, AgentMode.EDIT, result
        )
        _block(path, State.IMPLEMENTING, "IMPLEMENTER_UNAVAILABLE", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, None, None, messages)

    return _post_change_pipeline(
        repo,
        path,
        tf,
        decision,
        policy,
        skip_review,
        review_advisory,
        messages,
        worktree,
        from_state=State.IMPLEMENTING,
        commit_message=f"Implement task {task_id}",
    )


def _post_change_pipeline(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    skip_review: bool,
    review_advisory: bool,
    messages: list[str],
    worktree: Path,
    *,
    from_state: State,
    commit_message: str,
) -> StartResult:
    task_id = tf.frontmatter.id
    timeout = _timeout(policy)

    passed, verify_out = run_verify(worktree)
    _emit_verify_result(path, messages, task_id, passed, verify_out)
    if not passed:
        if from_state is State.IMPLEMENTING:
            _apply(path, State.IMPLEMENTING, Trigger.VERIFY_FAILED, decision, messages)
        return StartResult(task_id, from_state, worktree, False, None, messages)

    head = gitops.commit_all(worktree, commit_message)
    _emit(messages, task_id, f"commit {_short(head)}")

    try:
        head = gitops.rebase_onto_base(worktree, BASE_REF)
    except RuntimeError as exc:
        _emit(messages, task_id, f"rebase onto {BASE_REF}... conflict")
        append_section(path, "Rebase", str(exc))
        _block(path, from_state, "REBASE_CONFLICT", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, True, None, messages)
    _emit(messages, task_id, f"rebase onto {BASE_REF}... clean")

    passed, verify_out = run_verify(worktree)
    _emit_verify_result(path, messages, task_id, passed, verify_out)
    if not passed:
        if from_state is State.IMPLEMENTING:
            _apply(path, State.IMPLEMENTING, Trigger.VERIFY_FAILED, decision, messages)
            return StartResult(
                task_id, State.IMPLEMENTING, worktree, False, None, messages
            )
        return StartResult(task_id, from_state, worktree, False, None, messages)

    actual_paths = changed_files(worktree, BASE_REF)
    gate = check_merge_gate(decision, actual_paths, policy)
    if not gate.passed:
        _emit(messages, task_id, "merge gate... FAIL")
        tf = _apply(path, from_state, Trigger.RISK_ESCALATION, decision, messages)
        append_section(path, "Merge gate", gate.reason or "raised")
        return StartResult(task_id, State.TRIAGE, worktree, True, None, messages)
    _emit(messages, task_id, "merge gate... OK")

    need_review = decision.review_required and not skip_review
    if from_state is State.REWORK:
        tf = _apply(path, State.REWORK, Trigger.REWORK_DONE, decision, messages)
    elif not need_review:
        tf = _apply(
            path, State.IMPLEMENTING, Trigger.IMPLEMENT_DONE, decision, messages
        )
        _ready_report(tf, decision, True, 0, True, gate, messages, worktree)
        return StartResult(
            task_id, State(tf.frontmatter.state), worktree, True, None, messages
        )
    else:
        tf = _apply(
            path, State.IMPLEMENTING, Trigger.IMPLEMENT_DONE, decision, messages
        )

    if not need_review:
        # REWORK without review requirement still lands in REVIEW via REWORK_DONE;
        # skip reviewer and treat as clean readiness from REVIEW.
        tf = _apply(path, State.REVIEW, Trigger.REVIEW_CLEAN, decision, messages)
        _ready_report(tf, decision, True, 0, True, gate, messages, worktree)
        return StartResult(
            task_id, State(tf.frontmatter.state), worktree, True, None, messages
        )

    return _finish_review(
        repo,
        path,
        tf,
        decision,
        skip_review,
        review_advisory,
        messages,
        worktree,
        head,
        timeout,
        gate,
    )


def _resume_review(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    skip_review: bool,
    review_advisory: bool,
    messages: list[str],
    worktree: Path,
) -> StartResult:
    task_id = tf.frontmatter.id
    timeout = _timeout(policy)

    passed, verify_out = run_verify(worktree)
    _emit_verify_result(path, messages, task_id, passed, verify_out)
    if not passed:
        return StartResult(task_id, State.REVIEW, worktree, False, None, messages)

    journal = f".devflow/tasks/{task_id}.md"
    head = gitops.commit_paths(worktree, f"Update task {task_id} journal", journal)
    _emit(messages, task_id, f"commit {_short(head)}")

    try:
        head = gitops.rebase_onto_base(worktree, BASE_REF)
    except RuntimeError as exc:
        _emit(messages, task_id, f"rebase onto {BASE_REF}... conflict")
        append_section(path, "Rebase", str(exc))
        _block(path, State.REVIEW, "REBASE_CONFLICT", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, True, None, messages)
    _emit(messages, task_id, f"rebase onto {BASE_REF}... clean")

    passed, verify_out = run_verify(worktree)
    _emit_verify_result(path, messages, task_id, passed, verify_out)
    if not passed:
        return StartResult(task_id, State.REVIEW, worktree, False, None, messages)

    actual_paths = changed_files(worktree, BASE_REF)
    gate = check_merge_gate(decision, actual_paths, policy)
    if not gate.passed:
        _emit(messages, task_id, "merge gate... FAIL")
        tf = _apply(path, State.REVIEW, Trigger.RISK_ESCALATION, decision, messages)
        append_section(path, "Merge gate", gate.reason or "raised")
        return StartResult(task_id, State.TRIAGE, worktree, True, None, messages)
    _emit(messages, task_id, "merge gate... OK")

    if skip_review or not decision.review_required:
        tf = _apply(path, State.REVIEW, Trigger.REVIEW_CLEAN, decision, messages)
        _ready_report(tf, decision, True, 0, True, gate, messages, worktree)
        return StartResult(
            task_id, State(tf.frontmatter.state), worktree, True, None, messages
        )

    return _finish_review(
        repo,
        path,
        tf,
        decision,
        skip_review,
        review_advisory,
        messages,
        worktree,
        head,
        timeout,
        gate,
    )


def _finish_review(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    skip_review: bool,
    review_advisory: bool,
    messages: list[str],
    worktree: Path,
    head: str,
    timeout: float,
    gate: Any,
) -> StartResult:
    _ = skip_review
    task_id = tf.frontmatter.id
    record, tf = _run_review(
        repo, path, tf, worktree, head, timeout, messages, decision
    )
    if State(tf.frontmatter.state) is State.BLOCKED:
        return StartResult(task_id, State.BLOCKED, worktree, True, None, messages)
    blocking = 0 if record is None else record.blocking_findings
    if record is not None and blocking and not review_advisory:
        tf = _apply(
            path,
            State.REVIEW,
            Trigger.REVIEW_BLOCKING,
            decision,
            messages,
            cycles=review_cycle_count(tf),
        )
        _emit(
            messages,
            task_id,
            f"next: fix findings in the worktree, then run: devflow resume {task_id}",
        )
        _emit(
            messages,
            task_id,
            "(automatic rework is not implemented in V1)",
        )
    elif State(tf.frontmatter.state) is State.REVIEW:
        tf = _apply(path, State.REVIEW, Trigger.REVIEW_CLEAN, decision, messages)
    _ready_report(tf, decision, True, blocking, True, gate, messages, worktree)
    return StartResult(
        task_id, State(tf.frontmatter.state), worktree, True, record, messages
    )


def _prepare_resume_context(
    repo: Path, task_id: int, messages: list[str]
) -> tuple[Path, TaskFile, Path]:
    worktree = gitops.task_worktree(repo, task_id)
    if not worktree.is_dir():
        raise RunnerError(
            f"RESUME_CONTEXT_MISSING: task {task_id} has no worktree "
            f"({worktree.as_posix()})"
        )
    branch = gitops.task_branch_name(repo, task_id)
    if not branch:
        raise RunnerError(f"RESUME_CONTEXT_MISSING: task {task_id} has no task branch")
    path = task_path(worktree, task_id)
    if not path.is_file():
        raise RunnerError(
            f"RESUME_CONTEXT_MISSING: task {task_id} has no task file in worktree"
        )
    _emit(messages, task_id, f"worktree {worktree.relative_to(repo).as_posix()}")
    return path, read(path), worktree


def _resume_state_error(task_id: int, current: State) -> str:
    hints = {
        State.BACKLOG: f"use: devflow start {task_id}",
        State.PLAN_APPROVAL: f"use: devflow approve {task_id}",
        State.BLOCKED: "resolve the block first",
        State.READY_TO_MERGE: "task is already ready for merge",
        State.MERGED: "task is already merged",
        State.CANCELLED: "task is cancelled",
        State.TRIAGE: f"task {task_id} is TRIAGE; finish triage before resume",
    }
    hint = hints.get(current, "resume supports IMPLEMENTING, REWORK, REVIEW")
    return f"task {task_id} is {current.value}; {hint}"


def _write_plan(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    messages: list[str],
) -> StartResult | tuple[TaskFile, Path]:
    task_id = tf.frontmatter.id
    try:
        worktree = _ensure_worktree(repo, task_id, tf.frontmatter.title, messages)
    except WorktreeSetupError as exc:
        return _setup_worktree_blocked(path, tf, decision, messages, exc)
    role_root = worktree if (worktree / ".ai" / "roles").is_dir() else repo
    prompt = build_prompt(
        "implementer",
        tf,
        role_root,
        {"plan_detail": decision.plan_detail},
        plan_only=True,
    )
    result = _run_agent(
        repo,
        task_id,
        decision.implementer,
        prompt,
        worktree,
        AgentMode.READ_ONLY,
        _timeout(policy),
        path,
        _runtime_choice(tf, "implementer", decision.implementer, decision),
    )
    _emit_agent_result(
        messages,
        task_id,
        "implementer",
        decision.implementer,
        AgentMode.READ_ONLY,
        result,
    )
    if result.status is not AgentStatus.OK:
        _record_agent_error(
            path,
            "Implementer error",
            decision.implementer,
            AgentMode.READ_ONLY,
            result,
        )
        current = State(tf.frontmatter.state)
        _block(path, current, "IMPLEMENTER_UNAVAILABLE", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, None, None, messages)
    tf = append_section(path, "Plan", result.output.strip() or "(empty)")
    return tf, worktree


def _ensure_worktree(repo: Path, task_id: int, title: str, messages: list[str]) -> Path:
    resume = gitops.inspect_resume(task_id, repo)
    worktree = gitops.ensure_task_worktree(repo, task_id, title, BASE_REF)
    rel = worktree.resolve().relative_to(repo.resolve()).as_posix()
    if resume.worktree_exists:
        _emit(messages, task_id, f"worktree {rel} (existing)")
        return worktree
    _emit(messages, task_id, f"worktree {rel}")
    try:
        run_setup_worktree(worktree)
    except WorktreeSetupError:
        gitops.remove_task_worktree(repo, task_id)
        raise
    return worktree


def _setup_worktree_blocked(
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    messages: list[str],
    exc: WorktreeSetupError,
) -> StartResult:
    task_id = tf.frontmatter.id
    _emit(messages, task_id, "setup-worktree... FAIL")
    append_section(path, "Worktree setup", str(exc) or "setup-worktree failed")
    _block(
        path,
        State(tf.frontmatter.state),
        "SETUP_WORKTREE_FAILED",
        decision,
        messages,
    )
    return StartResult(task_id, State.BLOCKED, None, None, None, messages)


def _record_floor(path: Path, tf: TaskFile, policy: dict[str, Any]) -> TaskFile:
    _epic, floor, *_rest = decision_inputs(tf, policy)
    return update_frontmatter(
        path,
        floor_risk=floor.risk_floor,
        floor_matched=format_estimated_floor_matches(floor.matched_rules, tf),
    )


def _require_plan(tf: TaskFile) -> None:
    if "Plan" not in body_sections(tf):
        raise RunnerError(f"task {tf.frontmatter.id} has no plan to approve")


def _run_triage(
    repo: Path,
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    risk_hint: Risk | None,
    messages: list[str],
) -> tuple[TaskFile, RoutingDecision]:
    task_id = tf.frontmatter.id
    epic, floor, *_rest = decision_inputs(tf, policy)
    needed = needed_triage_fields(floor, epic)
    worktree = gitops.task_worktree(repo, task_id)
    agent_cwd = worktree if worktree.is_dir() else repo
    role_root = agent_cwd if (agent_cwd / ".ai" / "roles").is_dir() else repo
    prompt = build_prompt("triage", tf, role_root, {"needed": needed})
    agent = triage_provider(policy)
    defined = agents_api._defined_modes(agent)
    if defined and AgentMode.READ_ONLY not in defined:
        result = AgentResult(
            AgentStatus.BLOCKED,
            "",
            f"{agent} does not support mode {AgentMode.READ_ONLY.value}",
            0.0,
        )
    else:
        result = _run_agent(
            repo,
            task_id,
            agent,
            prompt,
            agent_cwd,
            AgentMode.READ_ONLY,
            _timeout(policy),
            path,
            _runtime_choice(tf, "triage", agent, decision),
        )
    _emit_agent_result(messages, task_id, "triage", agent, AgentMode.READ_ONLY, result)
    if result.status is not AgentStatus.OK:
        _record_agent_error(path, "Triage error", agent, AgentMode.READ_ONLY, result)
        tf = _block(path, State.TRIAGE, "AGENT_BLOCKED", decision, messages)
        return tf, decision
    try:
        signals, complexity, architecture_impact, uncertain = parse_triage_output(
            result.output
        )
    except RunnerError as exc:
        append_section(path, "Triage output error", str(exc))
        append_section(path, "Triage raw output", _truncate_agent_output(result.output))
        tf = _block(path, State.TRIAGE, "TRIAGE_INVALID_OUTPUT", decision, messages)
        _emit(messages, task_id, "blocked: TRIAGE_INVALID_OUTPUT")
        return tf, decision
    tf = update_frontmatter(
        path,
        signals=signals,
        complexity_proposed=complexity,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
    )
    decision = _decide(tf, policy, risk_hint)
    return tf, decision


def _run_review(
    repo: Path,
    path: Path,
    tf: TaskFile,
    worktree: Path,
    head_sha: str,
    timeout: float,
    messages: list[str],
    decision: RoutingDecision,
) -> tuple[ReviewRecord | None, TaskFile]:
    task_id = tf.frontmatter.id
    round_no = len(tf.frontmatter.review_records) + 1
    try:
        base_sha = gitops.git_output(worktree, "rev-parse", BASE_REF)
    except RuntimeError:
        base_sha = gitops.git_output(worktree, "rev-parse", "HEAD")
    try:
        diff = gitops.git_output(worktree, "diff", f"{BASE_REF}...HEAD")
    except RuntimeError:
        diff = ""
    previous = _previous_findings(tf)
    role_root = worktree if (worktree / ".ai" / "roles").is_dir() else repo
    prompt = build_prompt(
        "reviewer",
        tf,
        role_root,
        {
            "diff": diff or "(none)",
            "verify_output": "(see Verify sections)",
            "previous_findings": previous,
        },
    )
    review_wt: Path | None = None
    try:
        review_wt = gitops.add_review_worktree(repo, task_id, round_no, head_sha)
        result = _run_agent(
            repo,
            task_id,
            "claude",
            prompt,
            review_wt,
            AgentMode.REVIEW,
            timeout,
            path,
            _runtime_choice(tf, "reviewer", "claude", decision),
        )
        _emit_agent_result(
            messages, task_id, "reviewer", "claude", AgentMode.REVIEW, result
        )
        if result.status is not AgentStatus.OK:
            _record_agent_error(
                path, "Review error", "claude", AgentMode.REVIEW, result
            )
            tf = _block(path, State.REVIEW, "REVIEWER_UNAVAILABLE", decision, messages)
            return None, tf
        try:
            findings = parse_findings(result.output) if result.output.strip() else []
        except (ValueError, yaml.YAMLError) as exc:
            append_section(
                path,
                "Review error",
                f"status: {result.status.value}\ndetail: {exc}\n",
            )
            tf = _block(
                path, State.REVIEW, "REVIEWER_INVALID_OUTPUT", decision, messages
            )
            return None, tf
        blocking = sum(1 for item in findings if item.blocking)
        unverified = sum(
            1
            for item in findings
            if not item.verified and item.severity in {"BLOCKER", "HIGH"}
        )
        _emit(messages, task_id, f"{blocking} blocking, {unverified} unverified HIGH")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        record = ReviewRecord(
            head_sha=head_sha,
            base_sha=base_sha,
            round=round_no,
            blocking_findings=blocking,
            unverified_high=unverified,
            timestamp=stamp,
        )
        records = [*tf.frontmatter.review_records, record]
        tf = update_frontmatter(path, review_records=records)
        if findings:
            dumped = yaml.safe_dump(
                [
                    {
                        "id": item.id,
                        "severity": item.severity,
                        "verified": item.verified,
                        "blocking": item.blocking,
                        "problem": item.problem,
                    }
                    for item in findings
                ],
                sort_keys=False,
            )
            tf = append_section(path, f"Review — round {round_no}", dumped)
        _copy_review_evidence(worktree, review_wt, head_sha, round_no)
        return record, tf
    finally:
        if review_wt is not None:
            gitops.remove_worktree(repo, review_wt)
            gitops.delete_local_branch(repo, f"review/{task_id}-r{round_no}")


def _copy_review_evidence(
    task_wt: Path, review_wt: Path, start_sha: str, round_no: int
) -> None:
    try:
        names = gitops.git_output(review_wt, "diff", "--name-only", start_sha)
    except RuntimeError:
        return
    copied = False
    for rel in names.splitlines():
        rel = rel.replace("\\", "/")
        src = review_wt / rel
        if not src.is_file():
            continue
        dest = task_wt / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied = True
    if copied:
        gitops.commit_all(task_wt, f"Add review evidence for R-{round_no}")


def _dry_run(
    repo: Path,
    tf: TaskFile,
    current: State,
    risk_hint: Risk | None,
    skip_review: bool,
    review_advisory: bool,
    reason: str | None,
    after_approval: bool,
) -> StartResult:
    messages: list[str] = []
    task_id = tf.frontmatter.id
    if after_approval and current is not State.PLAN_APPROVAL:
        raise RunnerError(f"task {task_id} is {current.value}, expected PLAN_APPROVAL")
    if not after_approval and current is not State.BACKLOG:
        raise RunnerError(f"task {task_id} is {current.value}, expected BACKLOG")
    blockers = _unmerged_blockers(repo, tf)
    if blockers:
        listed = ", ".join(str(item) for item in blockers)
        raise RunnerError(f"blocked by unmerged tasks: {listed}")
    policy = load_policy_from_base(repo, BASE_REF)
    _emit(messages, task_id, f"policy from {BASE_REF}")
    decision = _decide(tf, policy, risk_hint)
    _emit(
        messages,
        task_id,
        (
            f"risk {decision.risk.value}, complexity {decision.complexity.value}"
            f" -> {decision.implementer}, plan {decision.plan_detail}"
        ),
    )
    _require_bypass_reason(decision, skip_review, review_advisory, reason)
    if after_approval:
        _require_plan(tf)
        target = State.IMPLEMENTING
    else:
        target = initial_state(decision, _has_epic(tf))
    _emit(messages, task_id, f"{current.value} -> {target.value} (dry-run)")
    if target is State.TRIAGE:
        agent = triage_provider(policy)
        _emit(messages, task_id, f"would run triage ({agent}, read_only)")
        _warn_unconfigured(messages, task_id, agent, "AGENT_BLOCKED")
    else:
        if decision.plan_required or target is State.IMPLEMENTING:
            rel = gitops.task_worktree(repo, task_id)
            try:
                shown = rel.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError:
                shown = rel.as_posix()
            _emit(messages, task_id, f"would open worktree {shown}")
        if decision.plan_required and not after_approval:
            _emit(
                messages,
                task_id,
                (
                    f"would run implementer ({decision.implementer}, read_only)"
                    " to write the plan"
                ),
            )
            _warn_unconfigured(
                messages, task_id, decision.implementer, "IMPLEMENTER_UNAVAILABLE"
            )
        if target is State.PLAN_APPROVAL:
            _emit(messages, task_id, f"would stop for: devflow approve {task_id}")
        else:
            _emit(
                messages,
                task_id,
                f"would run implementer ({decision.implementer}, edit)",
            )
            _warn_unconfigured(
                messages, task_id, decision.implementer, "IMPLEMENTER_UNAVAILABLE"
            )
            if decision.review_required and not skip_review:
                _emit(messages, task_id, "would run reviewer (claude, review)")
                _warn_unconfigured(messages, task_id, "claude", "REVIEWER_UNAVAILABLE")
            else:
                _emit(messages, task_id, "would skip review")
    _emit(messages, task_id, "dry-run: no lock, no worktree, no agent, no file changes")
    return StartResult(task_id, current, None, None, None, messages)


def parse_triage_output(
    output: str,
) -> tuple[TriageSignals, Complexity, ArchitectureImpact, bool]:
    chunks = [match.group(1) for match in _FENCE_RE.finditer(output)]
    raw = chunks[0] if chunks else output
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RunnerError(f"invalid triage output: YAML parse failed ({exc})") from exc
    if not isinstance(data, dict):
        raise RunnerError("triage output is not a YAML mapping")
    signals_raw = data.get("signals")
    if not isinstance(signals_raw, dict):
        raise RunnerError("invalid triage signals: must be a mapping")
    required_signals = (
        "transaction_change",
        "concurrency_sensitive",
        "architecture_boundary_change",
        "unfamiliar_area",
    )
    missing = [name for name in required_signals if name not in signals_raw]
    if missing:
        raise RunnerError("invalid triage signals: missing " + ", ".join(missing))
    signals = TriageSignals(
        transaction_change=_parse_triage_bool(
            signals_raw["transaction_change"], "signals.transaction_change"
        ),
        concurrency_sensitive=_parse_triage_bool(
            signals_raw["concurrency_sensitive"], "signals.concurrency_sensitive"
        ),
        architecture_boundary_change=_parse_triage_bool(
            signals_raw["architecture_boundary_change"],
            "signals.architecture_boundary_change",
        ),
        unfamiliar_area=_parse_triage_bool(
            signals_raw["unfamiliar_area"], "signals.unfamiliar_area"
        ),
    )
    if "complexity" not in data:
        raise RunnerError("invalid triage complexity: missing")
    if "architecture_impact" not in data:
        raise RunnerError("invalid triage architecture_impact: missing")
    if "uncertain" not in data:
        raise RunnerError("invalid triage uncertain: missing")
    complexity = _parse_triage_complexity(data["complexity"])
    architecture_impact = _parse_triage_architecture_impact(data["architecture_impact"])
    uncertain = _parse_triage_bool(data["uncertain"], "uncertain")
    return signals, complexity, architecture_impact, uncertain


def _parse_triage_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise RunnerError(
        f"invalid triage {field}: expected YAML boolean true/false, got {value!r}"
    )


def _parse_triage_complexity(value: object) -> Complexity:
    if isinstance(value, bool) or value is None:
        raise RunnerError(f"invalid triage complexity: {value!r}")
    text = str(value).strip().upper()
    try:
        return Complexity(text)
    except ValueError as exc:
        raise RunnerError(f"invalid triage complexity: {value}") from exc


def _parse_triage_architecture_impact(value: object) -> ArchitectureImpact:
    # PyYAML 1.1 may load unquoted YES as boolean True.
    if value is True:
        return ArchitectureImpact.YES
    if isinstance(value, bool) or value is None:
        raise RunnerError(f"invalid triage architecture_impact: {value!r}")
    text = str(value).strip().upper()
    try:
        return ArchitectureImpact(text)
    except ValueError as exc:
        raise RunnerError(f"invalid triage architecture_impact: {value}") from exc


def _truncate_agent_output(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]...\n"


def _run_agent(
    repo: Path,
    task_id: int,
    agent: str,
    prompt: str,
    worktree: Path,
    mode: AgentMode,
    timeout: float,
    task_path: Path,
    runtime: RuntimeChoice,
) -> AgentResult:
    prompt_file = _write_prompt(prompt)
    activity = _terminal_activity(task_id, f"{agent} {mode.value}")
    heartbeat_stop = threading.Event()
    heartbeat: threading.Thread | None = None
    if activity is not None:
        heartbeat = threading.Thread(
            target=_runtime_heartbeat,
            args=(activity, heartbeat_stop),
            daemon=True,
        )
        heartbeat.start()
    try:

        def on_spawn(pid: int) -> None:
            set_agent_pid(task_id, repo, pid)
            if activity is not None:
                activity(f"started · pid {pid}")

        result = agents_api.run(
            agent,
            prompt_file,
            worktree,
            mode,
            timeout_minutes=timeout,
            env=sanitized_env(),
            on_spawn=on_spawn,
            on_activity=activity,
            model=runtime.model,
            effort=runtime.effort,
        )
    finally:
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1)
        set_agent_pid(task_id, repo, None)
        prompt_file.unlink(missing_ok=True)
    findings = check_agent_output_for_violations(result.output)
    if findings:
        append_section(task_path, "Agent output", "\n".join(findings))
    return result


def _write_prompt(text: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix="devflow-prompt-", suffix=".md")
    os.close(fd)
    path = Path(raw)
    path.write_text(text, encoding="utf-8")
    return path


def _decide(
    tf: TaskFile, policy: dict[str, Any], risk_hint: Risk | None
) -> RoutingDecision:
    epic, floor, signals, complexity, architecture_impact, uncertain = decision_inputs(
        tf, policy
    )
    return decide(
        floor=floor,
        signals=signals,
        complexity=complexity,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
        user_risk_hint=risk_hint,
        paths=estimate_paths(tf),
        policy=policy,
        epic=epic,
    )


def _apply(
    path: Path,
    current: State,
    trigger: Trigger,
    decision: RoutingDecision,
    messages: list[str],
    *,
    has_epic: bool = False,
    cycles: int = 0,
) -> TaskFile:
    tf = read(path)
    result = transition(
        current,
        trigger,
        decision,
        has_epic_decision=has_epic or _has_epic(tf),
        review_cycles=cycles,
        blocked_from=(
            State(tf.frontmatter.blocked_from) if tf.frontmatter.blocked_from else None
        ),
    )
    updates: dict[str, object] = {"state": result.to_state.value}
    if result.to_state is State.BLOCKED:
        updates["blocked_from"] = result.from_state.value
    elif result.from_state is State.BLOCKED:
        updates["blocked_from"] = None
        updates["blocked_reason"] = None
    tf = update_frontmatter(path, **updates)
    append_section(
        path,
        "Transition",
        f"{result.from_state.value} → {result.to_state.value} ({result.trigger.value})",
    )
    _emit(
        messages,
        tf.frontmatter.id,
        f"{result.from_state.value} -> {result.to_state.value}",
    )
    return read(path)


def _block(
    path: Path,
    from_state: State,
    blocked_reason: str,
    decision: RoutingDecision,
    messages: list[str],
) -> TaskFile:
    tf = _apply(path, from_state, Trigger.AGENT_BLOCKED, decision, messages)
    tf = update_frontmatter(path, blocked_reason=blocked_reason)
    return tf


def _require_bypass_reason(
    decision: RoutingDecision,
    skip_review: bool,
    review_advisory: bool,
    reason: str | None,
) -> None:
    if not (skip_review or review_advisory):
        return
    if decision.bypass_friction == "reason" and not (reason and reason.strip()):
        raise RunnerError("--reason is required when bypass friction is reason")


def _unmerged_blockers(repo: Path, tf: TaskFile) -> list[int]:
    open_ids: list[int] = []
    for blocker_id in tf.frontmatter.blocked_by:
        blocker_path = resolve_task(repo, blocker_id, attach=False)
        if blocker_path is None:
            open_ids.append(blocker_id)
            continue
        other = read(blocker_path)
        if other.frontmatter.state != State.MERGED.value:
            open_ids.append(blocker_id)
    return open_ids


def _has_epic(tf: TaskFile) -> bool:
    fm = tf.frontmatter
    return fm.risk_proposed is not None and fm.complexity_proposed is not None


def _timeout(policy: dict[str, Any]) -> float:
    routing = policy.get("routing") or {}
    raw = routing.get("agent_timeout_minutes", 20)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 20.0


def _runtime_providers(
    decision: RoutingDecision, policy: dict[str, Any]
) -> dict[str, str]:
    return {
        "triage": triage_provider(policy),
        "implementer": decision.implementer,
        "reviewer": review_provider(policy),
    }


def _runtime_capabilities(
    providers: dict[str, str],
) -> dict[str, ProviderCapabilities]:
    return {
        provider: discover_provider(provider)
        for provider in dict.fromkeys(providers.values())
    }


def _recommended_runtime(
    decision: RoutingDecision,
    providers: dict[str, str],
    capabilities: dict[str, ProviderCapabilities],
) -> RuntimeSelection:
    choices: dict[str, RuntimeChoice] = {}
    for role in RUNTIME_ROLES:
        capability = capabilities[providers[role]]
        recommended = recommended_effort(decision.complexity, role)
        effort = recommended if recommended in capability.efforts else None
        choices[role] = RuntimeChoice(effort=effort)
    return RuntimeSelection(**choices)


def _ensure_runtime_selection(
    path: Path,
    tf: TaskFile,
    decision: RoutingDecision,
    policy: dict[str, Any],
    messages: list[str],
    selector: RuntimeSelector | None,
) -> TaskFile:
    providers = _runtime_providers(decision, policy)
    capabilities = _runtime_capabilities(providers)
    selection = tf.frontmatter.runtime_selection
    if selection is None:
        selection = _recommended_runtime(decision, providers, capabilities)
        if selector is not None:
            selection = selector(decision, providers, selection, capabilities)
        tf = update_frontmatter(path, runtime_selection=selection)
    selection = _applicable_runtime(
        tf.frontmatter.id, selection, providers, capabilities, messages
    )
    if selection != tf.frontmatter.runtime_selection:
        tf = update_frontmatter(path, runtime_selection=selection)
    _emit(messages, tf.frontmatter.id, "AI runtime")
    for role in RUNTIME_ROLES:
        choice = selection.for_role(role)
        model = choice.model or "provider default"
        effort = choice.effort or "provider-managed"
        _emit(
            messages,
            tf.frontmatter.id,
            f"  {role}: {providers[role]} · {model} · {effort}",
        )
    return tf


def _applicable_runtime(
    task_id: int,
    selection: RuntimeSelection,
    providers: dict[str, str],
    capabilities: dict[str, ProviderCapabilities],
    messages: list[str],
) -> RuntimeSelection:
    choices: dict[str, RuntimeChoice] = {}
    for role in RUNTIME_ROLES:
        provider = providers[role]
        capability = capabilities[provider]
        choice = selection.for_role(role)
        model = choice.model
        effort = choice.effort
        if model is not None and _obviously_incompatible_model(provider, model):
            _emit(
                messages,
                task_id,
                (
                    f"WARNING — stored {role} model {model!r} is incompatible "
                    f"with policy provider {provider}; using provider default"
                ),
            )
            model = None
        if (
            effort is not None
            and capability.available
            and effort not in capability.efforts
        ):
            _emit(
                messages,
                task_id,
                (
                    f"WARNING — stored {role} effort {effort!r} is unsupported "
                    f"by {provider}; using provider-managed effort"
                ),
            )
            effort = None
        choices[role] = RuntimeChoice(model=model, effort=effort)
    return RuntimeSelection(**choices)


def _obviously_incompatible_model(provider: str, model: str) -> bool:
    lowered = model.casefold()
    if provider == "claude":
        return lowered.startswith(("gpt-", "o1", "o3", "o4"))
    if provider == "codex":
        return lowered.startswith("claude-") or lowered in {
            "fable",
            "opus",
            "sonnet",
            "haiku",
        }
    return provider == "cursor"


def _runtime_choice(
    tf: TaskFile,
    role: str,
    provider: str,
    decision: RoutingDecision,
) -> RuntimeChoice:
    selection = tf.frontmatter.runtime_selection
    if selection is not None:
        return selection.for_role(role)
    capability = discover_provider(provider)
    effort = recommended_effort(decision.complexity, role)
    return RuntimeChoice(effort=effort if effort in capability.efforts else None)


def _emit(messages: list[str], task_id: int, line: str) -> None:
    text = f"{task_id}: {line}"
    messages.append(text)
    print(text, flush=True)


def _terminal_activity(task_id: int, phase: str) -> Callable[[str], None] | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    def emit(line: str) -> None:
        if line:
            print(f"{task_id}:   {phase}: {line}", flush=True)

    return emit


def _runtime_heartbeat(
    activity: Callable[[str], None],
    stop: threading.Event,
    interval_seconds: float = 10.0,
) -> None:
    started = time.monotonic()
    while not stop.wait(interval_seconds):
        elapsed = int(time.monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        activity(f"working · elapsed {minutes:02d}:{seconds:02d}")


def _emit_agent_result(
    messages: list[str],
    task_id: int,
    role: str,
    agent: str,
    mode: AgentMode,
    result: AgentResult,
) -> None:
    mark = "✓" if result.status is AgentStatus.OK else "✗"
    _emit(
        messages,
        task_id,
        (f"{mark} {role} ({agent}, {mode.value}) · {result.duration_seconds:.1f}s"),
    )
    if result.status is not AgentStatus.OK:
        for line in (result.detail or result.status.value).splitlines():
            _emit(messages, task_id, f"  {line}")


def _record_agent_error(
    path: Path,
    heading: str,
    agent: str,
    mode: AgentMode,
    result: AgentResult,
) -> None:
    detail = result.detail or result.status.value
    append_section(
        path,
        heading,
        (
            f"provider: {agent}\n"
            f"mode: {mode.value}\n"
            f"status: {result.status.value}\n"
            f"detail: {_truncate_agent_output(detail, limit=2000)}\n"
        ),
    )


def _warn_unconfigured(
    messages: list[str], task_id: int, agent: str, blocked_reason: str
) -> None:
    env_name = COMMANDS.get(agent)
    if env_name is None:
        return
    if _resolve_command(agent) is not None:
        return
    _emit(
        messages,
        task_id,
        f"WARNING — {agent} is not configured (set {env_name})",
    )
    _emit(
        messages,
        task_id,
        f"a real run would stop with BLOCKED: {blocked_reason}",
    )


def _short(sha: str) -> str:
    return sha[:7] if len(sha) >= 7 else sha


def _previous_findings(tf: TaskFile) -> str:
    records = tf.frontmatter.review_records
    if not records:
        return "(none)"
    lines = [
        (
            f"round {item.round}: {item.blocking_findings} blocking, "
            f"{item.unverified_high} unverified HIGH"
        )
        for item in records
    ]
    return "\n".join(lines)


def _ready_report(
    tf: TaskFile,
    decision: RoutingDecision,
    verify_passed: bool,
    blocking: int,
    rebase_clean: bool,
    gate: Any,
    messages: list[str],
    worktree: Path,
) -> None:
    readiness = check_ready_to_merge(
        tf, decision, verify_passed, blocking, rebase_clean, gate
    )
    task_id = tf.frontmatter.id
    if readiness.ready:
        _emit(messages, task_id, "merge readiness: open")
        _emit(messages, task_id, "V1 does not open a pull request")
        if State(tf.frontmatter.state) is State.READY_TO_MERGE:
            journal = f".devflow/tasks/{task_id}.md"
            gitops.commit_paths(worktree, f"Finalize task {task_id} journal", journal)
        return
    _emit(
        messages,
        task_id,
        f"merge readiness: blocked ({len(readiness.blockers)})",
    )
    for blocker in readiness.blockers:
        _emit(messages, task_id, f"  - {blocker}")


def _stop_running_agent(task_id: int, repo: Path) -> None:
    lock = read_lock(task_id, repo)
    if lock is None:
        return
    if lock.agent_pid is not None:
        terminate_tree(lock.agent_pid)
    release(task_id, repo)
