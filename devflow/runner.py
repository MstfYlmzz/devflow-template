"""Orchestrate `devflow start`. Binds existing modules; invents no new rules."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
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
from devflow.ci_checks import changed_files
from devflow.freshness import ReviewRecord
from devflow.lock import (
    LockHeld,
    StaleLock,
    acquire,
    read_lock,
    release,
    set_agent_pid,
)
from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    Risk,
    RoutingDecision,
    TriageSignals,
    check_merge_gate,
    decide,
    needed_triage_fields,
)
from devflow.prompts import build_prompt
from devflow.review import parse_findings
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
    append_section,
    decision_inputs,
    estimate_paths,
    read,
    update_frontmatter,
)

BASE_REF = "origin/main"
_FENCE_RE = re.compile(r"```(?:yaml)?\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


class RunnerError(Exception):
    """A start/stop/cancel precondition failed."""


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
) -> StartResult:
    path = _task_path(repo, task_id)
    if not path.is_file():
        raise RunnerError(f"task file not found: {path}")
    tf = read(path)
    current = State(tf.frontmatter.state)
    if dry_run:
        return _dry_run(
            repo,
            tf,
            current,
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
            return StartResult(task_id, current, None, None, None, [str(exc)])
        except StaleLock as exc:
            return StartResult(
                task_id,
                current,
                None,
                None,
                None,
                [str(exc), f"run: devflow recover {task_id}"],
            )
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


def stop(repo: Path, task_id: int) -> StartResult:
    path = _task_path(repo, task_id)
    if not path.is_file():
        raise RunnerError(f"task file not found: {path}")
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
    path = _task_path(repo, task_id)
    if not path.is_file():
        raise RunnerError(f"task file not found: {path}")
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


def run_verify(worktree: Path) -> tuple[bool, str]:
    script = worktree / "scripts" / "verify"
    if not script.is_file():
        return False, "scripts/verify not found"
    argv = [str(script)]
    if os.name == "nt":
        argv = ["bash", str(script)]
    result = subprocess.run(
        argv,
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, f"{result.stdout}{result.stderr}"


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
) -> StartResult:
    messages: list[str] = []
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
    _require_bypass_reason(decision, skip_review, review_advisory, reason)

    if after_approval:
        tf = _apply(path, current, Trigger.PLAN_APPROVED, decision, messages)
        return _implement_onward(
            repo, path, tf, decision, policy, skip_review, review_advisory, messages
        )

    target = initial_state(decision, _has_epic(tf))
    tf = _apply(
        path, current, Trigger.START, decision, messages, has_epic=_has_epic(tf)
    )

    if target is State.TRIAGE:
        tf, decision = _run_triage(
            repo, path, tf, decision, policy, risk_hint, messages
        )
        if State(tf.frontmatter.state) is State.BLOCKED:
            return StartResult(task_id, State.BLOCKED, None, None, None, messages)
        target = State(tf.frontmatter.state)

    if target is State.PLAN_APPROVAL:
        _emit(messages, task_id, f"run: devflow approve {task_id}")
        return StartResult(task_id, State.PLAN_APPROVAL, None, None, None, messages)

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
    resume = gitops.inspect_resume(task_id, repo)
    worktree = gitops.ensure_task_worktree(repo, task_id, title, BASE_REF)
    rel = worktree.resolve().relative_to(repo.resolve()).as_posix()
    if resume.worktree_exists:
        _emit(messages, task_id, f"worktree {rel} (existing)")
    else:
        _emit(messages, task_id, f"worktree {rel}")

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
    )
    _emit(
        messages,
        task_id,
        (
            f"implementer ({decision.implementer}, edit)..."
            f" {int(result.duration_seconds)}s"
        ),
    )
    if result.status is AgentStatus.BLOCKED:
        _block(path, State.IMPLEMENTING, "IMPLEMENTER_UNAVAILABLE", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, None, None, messages)

    passed, verify_out = run_verify(worktree)
    _emit(messages, task_id, f"verify... {'PASS' if passed else 'FAIL'}")
    append_section(path, "Verify", verify_out.strip() or "(no output)")
    if not passed:
        _apply(path, State.IMPLEMENTING, Trigger.VERIFY_FAILED, decision, messages)
        return StartResult(task_id, State.IMPLEMENTING, worktree, False, None, messages)

    head = gitops.commit_all(worktree, f"Implement task {task_id}")
    _emit(messages, task_id, f"commit {_short(head)}")

    try:
        head = gitops.rebase_onto_base(worktree, BASE_REF)
    except RuntimeError as exc:
        _emit(messages, task_id, f"rebase onto {BASE_REF}... conflict")
        append_section(path, "Rebase", str(exc))
        _block(path, State.IMPLEMENTING, "REBASE_CONFLICT", decision, messages)
        return StartResult(task_id, State.BLOCKED, worktree, True, None, messages)
    _emit(messages, task_id, f"rebase onto {BASE_REF}... clean")

    passed, verify_out = run_verify(worktree)
    _emit(messages, task_id, f"verify... {'PASS' if passed else 'FAIL'}")
    append_section(path, "Verify", verify_out.strip() or "(no output)")
    if not passed:
        _apply(path, State.IMPLEMENTING, Trigger.VERIFY_FAILED, decision, messages)
        return StartResult(task_id, State.IMPLEMENTING, worktree, False, None, messages)

    actual_paths = changed_files(worktree, BASE_REF)
    gate = check_merge_gate(decision, actual_paths, policy)
    if not gate.passed:
        _emit(messages, task_id, "merge gate... FAIL")
        tf = _apply(
            path, State.IMPLEMENTING, Trigger.RISK_ESCALATION, decision, messages
        )
        append_section(path, "Merge gate", gate.reason or "raised")
        return StartResult(task_id, State.TRIAGE, worktree, True, None, messages)
    _emit(messages, task_id, "merge gate... OK")

    need_review = decision.review_required and not skip_review
    if not need_review:
        tf = _apply(
            path, State.IMPLEMENTING, Trigger.IMPLEMENT_DONE, decision, messages
        )
        _ready_report(tf, decision, True, 0, True, gate, messages)
        return StartResult(
            task_id, State(tf.frontmatter.state), worktree, True, None, messages
        )

    tf = _apply(path, State.IMPLEMENTING, Trigger.IMPLEMENT_DONE, decision, messages)
    record, tf = _run_review(repo, path, tf, worktree, head, timeout, messages)
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
            (f"next: fix findings in the worktree, then rerun devflow start {task_id}"),
        )
        _emit(
            messages,
            task_id,
            "(automatic rework is not implemented in V1)",
        )
    elif State(tf.frontmatter.state) is State.REVIEW:
        tf = _apply(path, State.REVIEW, Trigger.REVIEW_CLEAN, decision, messages)
    _ready_report(tf, decision, True, blocking, True, gate, messages)
    return StartResult(
        task_id, State(tf.frontmatter.state), worktree, True, record, messages
    )


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
    prompt = build_prompt("triage", tf, repo, {"needed": needed})
    result = _run_agent(
        repo,
        task_id,
        "cursor",
        prompt,
        repo,
        AgentMode.READ_ONLY,
        _timeout(policy),
        path,
    )
    _emit(
        messages,
        task_id,
        f"triage (cursor, read_only)... {int(result.duration_seconds)}s",
    )
    if result.status is AgentStatus.BLOCKED:
        tf = _block(path, State.TRIAGE, "AGENT_BLOCKED", decision, messages)
        return tf, decision
    signals, complexity, architecture_impact, uncertain = parse_triage_output(
        result.output
    )
    tf = update_frontmatter(
        path,
        signals=signals,
        complexity_proposed=complexity,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
    )
    decision = _decide(tf, policy, risk_hint)
    tf = _apply(path, State.TRIAGE, Trigger.TRIAGE_DONE, decision, messages)
    return tf, decision


def _run_review(
    repo: Path,
    path: Path,
    tf: TaskFile,
    worktree: Path,
    head_sha: str,
    timeout: float,
    messages: list[str],
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
    review_wt = gitops.add_review_worktree(repo, task_id, round_no, head_sha)
    result = _run_agent(
        repo, task_id, "claude", prompt, review_wt, AgentMode.REVIEW, timeout, path
    )
    _emit(
        messages,
        task_id,
        f"reviewer (claude, review)... {int(result.duration_seconds)}s",
    )
    findings = parse_findings(result.output) if result.output.strip() else []
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
    gitops.remove_worktree(repo, review_wt)
    gitops.delete_local_branch(repo, f"review/{task_id}-r{round_no}")
    return record, tf


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
        target = State.IMPLEMENTING
    else:
        target = initial_state(decision, _has_epic(tf))
    _emit(messages, task_id, f"{current.value} -> {target.value} (dry-run)")
    if target is State.PLAN_APPROVAL:
        _emit(messages, task_id, f"would stop for: devflow approve {task_id}")
    elif target is State.TRIAGE:
        _emit(messages, task_id, "would run triage (cursor, read_only)")
        _warn_unconfigured(messages, task_id, "cursor", "AGENT_BLOCKED")
    else:
        rel = gitops.task_worktree(repo, task_id)
        try:
            shown = rel.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            shown = rel.as_posix()
        _emit(messages, task_id, f"would open worktree {shown}")
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
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise RunnerError("triage output is not a YAML mapping")
    signals_raw = data.get("signals") or {}
    if not isinstance(signals_raw, dict):
        raise RunnerError("triage signals must be a mapping")
    signals = TriageSignals(
        transaction_change=bool(signals_raw.get("transaction_change", False)),
        concurrency_sensitive=bool(signals_raw.get("concurrency_sensitive", False)),
        architecture_boundary_change=bool(
            signals_raw.get("architecture_boundary_change", False)
        ),
        unfamiliar_area=bool(signals_raw.get("unfamiliar_area", False)),
    )
    complexity = Complexity(str(data.get("complexity", "MEDIUM")).strip().upper())
    architecture_impact = ArchitectureImpact(
        str(data.get("architecture_impact", "NONE")).strip().upper()
    )
    uncertain = bool(data.get("uncertain", False))
    return signals, complexity, architecture_impact, uncertain


def _run_agent(
    repo: Path,
    task_id: int,
    agent: str,
    prompt: str,
    worktree: Path,
    mode: AgentMode,
    timeout: float,
    task_path: Path,
) -> AgentResult:
    prompt_file = _write_prompt(prompt)
    try:

        def on_spawn(pid: int) -> None:
            set_agent_pid(task_id, repo, pid)

        result = agents_api.run(
            agent,
            prompt_file,
            worktree,
            mode,
            timeout_minutes=timeout,
            env=sanitized_env(),
            on_spawn=on_spawn,
        )
    finally:
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
        blocker_path = _task_path(repo, blocker_id)
        if not blocker_path.is_file():
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


def _task_path(repo: Path, task_id: int) -> Path:
    return repo / ".devflow" / "tasks" / f"{task_id}.md"


def _emit(messages: list[str], task_id: int, line: str) -> None:
    text = f"{task_id}: {line}"
    messages.append(text)
    print(text, flush=True)


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
) -> None:
    readiness = check_ready_to_merge(
        tf, decision, verify_passed, blocking, rebase_clean, gate
    )
    task_id = tf.frontmatter.id
    if readiness.ready:
        _emit(messages, task_id, "merge gate: open")
        _emit(messages, task_id, "V1 does not open a pull request")
        return
    _emit(messages, task_id, f"merge gate: blocked ({len(readiness.blockers)})")


def _stop_running_agent(task_id: int, repo: Path) -> None:
    lock = read_lock(task_id, repo)
    if lock is None:
        return
    if lock.agent_pid is not None:
        terminate_tree(lock.agent_pid)
    release(task_id, repo)
