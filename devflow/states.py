"""Pure task state transitions. Persistence goes through taskfile."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from devflow.freshness import (
    FreshnessResult,
    check_decision_validity,
    decision_validity_blockers,
    has_human_decision,
    latest_review_record,
)
from devflow.policy import MergeGateResult, Risk, RoutingDecision
from devflow.taskfile import TaskFile, body_sections, read_doc_impact

_REVIEW_ROUND_RE = re.compile(r"^Review — round \d+$")
_REVIEW_CYCLE_LIMIT = 2

# BLOCKED reasons stored on TaskFrontmatter.blocked_reason (not triggers):
# INTERRUPTED — the run stopped mid-flight and left a stale lock.


class State(enum.Enum):
    BACKLOG = "BACKLOG"
    TRIAGE = "TRIAGE"
    PLAN_APPROVAL = "PLAN_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    REVIEW = "REVIEW"
    REWORK = "REWORK"
    READY_TO_MERGE = "READY_TO_MERGE"
    BLOCKED = "BLOCKED"
    MERGED = "MERGED"
    CANCELLED = "CANCELLED"


class Trigger(enum.Enum):
    START = "start"
    TRIAGE_DONE = "triage_done"
    PLAN_APPROVED = "plan_approved"
    IMPLEMENT_DONE = "implement_done"
    VERIFY_FAILED = "verify_failed"
    REVIEW_CLEAN = "review_clean"
    REVIEW_BLOCKING = "review_blocking"
    REWORK_DONE = "rework_done"
    RISK_ESCALATION = "risk_escalation"
    COMPLEXITY_ESCALATION = "complexity_escalation"
    MERGE_GATE_RAISED = "merge_gate_raised"
    CI_FAILED = "ci_failed"
    AGENT_BLOCKED = "agent_blocked"
    HUMAN_RESOLVED = "human_resolved"
    MERGED = "merged"
    CANCEL = "cancel"


_TERMINAL = frozenset({State.MERGED, State.CANCELLED})
_ACTIVE = (
    State.BACKLOG,
    State.TRIAGE,
    State.PLAN_APPROVAL,
    State.IMPLEMENTING,
    State.REVIEW,
    State.REWORK,
    State.READY_TO_MERGE,
)


def _build_transitions() -> dict[tuple[State, Trigger], State]:
    table: dict[tuple[State, Trigger], State] = {
        (State.PLAN_APPROVAL, Trigger.PLAN_APPROVED): State.IMPLEMENTING,
        (State.IMPLEMENTING, Trigger.VERIFY_FAILED): State.IMPLEMENTING,
        (State.REVIEW, Trigger.REVIEW_CLEAN): State.READY_TO_MERGE,
        (State.REVIEW, Trigger.REVIEW_BLOCKING): State.REWORK,
        (State.REWORK, Trigger.REWORK_DONE): State.REVIEW,
        (State.READY_TO_MERGE, Trigger.MERGED): State.MERGED,
        (State.READY_TO_MERGE, Trigger.CI_FAILED): State.REWORK,
        (State.READY_TO_MERGE, Trigger.MERGE_GATE_RAISED): State.TRIAGE,
        (State.BLOCKED, Trigger.CANCEL): State.CANCELLED,
    }
    for state in _ACTIVE:
        table[(state, Trigger.RISK_ESCALATION)] = State.TRIAGE
        table[(state, Trigger.COMPLEXITY_ESCALATION)] = State.IMPLEMENTING
        table[(state, Trigger.AGENT_BLOCKED)] = State.BLOCKED
        table[(state, Trigger.CANCEL)] = State.CANCELLED
    return table


TRANSITIONS: dict[tuple[State, Trigger], State] = _build_transitions()


class InvalidTransition(Exception):
    def __init__(self, current: State, trigger: Trigger) -> None:
        self.current = current
        self.trigger = trigger
        super().__init__(f"cannot apply '{trigger.value}' from {current.value}")


@dataclass
class TransitionResult:
    from_state: State
    to_state: State
    trigger: Trigger
    reason: str


@dataclass
class MergeReadiness:
    ready: bool
    blockers: list[str]


def initial_state(decision: RoutingDecision, has_epic_decision: bool) -> State:
    if not has_epic_decision:
        return State.TRIAGE
    if decision.risk is Risk.HIGH:
        return State.PLAN_APPROVAL
    return State.IMPLEMENTING


def review_cycle_count(tf: TaskFile) -> int:
    return sum(1 for heading in body_sections(tf) if _REVIEW_ROUND_RE.match(heading))


def check_ready_to_merge(
    tf: TaskFile,
    decision: RoutingDecision,
    verify_passed: bool,
    blocking_findings: int,
    rebase_clean: bool,
    merge_gate: MergeGateResult,
    code_freshness: FreshnessResult | None = None,
) -> MergeReadiness:
    _ = decision
    blockers: list[str] = []
    if not verify_passed:
        blockers.append("verify did not pass")
    if blocking_findings:
        blockers.append(f"blocking findings present ({blocking_findings})")
    if not rebase_clean:
        blockers.append("rebase is not clean")
    try:
        impact = read_doc_impact(tf)
    except ValueError:
        blockers.append("doc impact section is unreadable")
    else:
        if impact is None:
            blockers.append("doc impact section is missing")
    if not merge_gate.passed:
        blockers.append(merge_gate.reason or "merge gate failed")
    if code_freshness is not None and not code_freshness.fresh:
        changed = code_freshness.changed_since_review
        if changed:
            blockers.append(
                f"code changed since review ({len(changed)} files) — re-review required"
            )
        else:
            blockers.append(code_freshness.reason)
    validity = check_decision_validity(tf)
    blockers.extend(decision_validity_blockers(validity))
    latest = latest_review_record(tf)
    if latest is not None and latest.unverified_high > 0 and not has_human_decision(tf):
        blockers.append(
            f"{latest.unverified_high} unverified HIGH findings need human decision"
        )
    return MergeReadiness(ready=not blockers, blockers=blockers)


def transition(
    current: State,
    trigger: Trigger,
    decision: RoutingDecision | None = None,
    *,
    has_epic_decision: bool = False,
    review_cycles: int = 0,
    blocked_from: State | None = None,
) -> TransitionResult:
    if current in _TERMINAL:
        raise InvalidTransition(current, trigger)

    if current is State.BACKLOG and trigger is Trigger.START:
        if decision is None:
            raise InvalidTransition(current, trigger)
        to_state = initial_state(decision, has_epic_decision)
        return _result(current, to_state, trigger)

    if current is State.TRIAGE and trigger is Trigger.TRIAGE_DONE:
        if decision is None:
            raise InvalidTransition(current, trigger)
        to_state = (
            State.PLAN_APPROVAL if decision.risk is Risk.HIGH else State.IMPLEMENTING
        )
        return _result(current, to_state, trigger)

    if current is State.IMPLEMENTING and trigger is Trigger.IMPLEMENT_DONE:
        if decision is None:
            raise InvalidTransition(current, trigger)
        to_state = State.REVIEW if decision.review_required else State.READY_TO_MERGE
        return _result(current, to_state, trigger)

    if current is State.BLOCKED and trigger is Trigger.HUMAN_RESOLVED:
        if blocked_from is None:
            raise InvalidTransition(current, trigger)
        return _result(current, blocked_from, trigger, "human resolved")

    if (
        current is State.REVIEW
        and trigger is Trigger.REVIEW_BLOCKING
        and review_cycles >= _REVIEW_CYCLE_LIMIT
    ):
        return _result(
            current,
            State.BLOCKED,
            trigger,
            "review cycle limit reached (2)",
        )

    dest = TRANSITIONS.get((current, trigger))
    if dest is None:
        raise InvalidTransition(current, trigger)
    return _result(current, dest, trigger)


def _result(
    current: State,
    to_state: State,
    trigger: Trigger,
    reason: str | None = None,
) -> TransitionResult:
    return TransitionResult(
        from_state=current,
        to_state=to_state,
        trigger=trigger,
        reason=reason or trigger.value.replace("_", " "),
    )
