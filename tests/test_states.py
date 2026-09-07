from __future__ import annotations

from pathlib import Path

import pytest

from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    MergeGateResult,
    Risk,
    RoutingDecision,
)
from devflow.states import (
    TRANSITIONS,
    InvalidTransition,
    State,
    Trigger,
    check_ready_to_merge,
    initial_state,
    review_cycle_count,
    transition,
)
from devflow.taskfile import append_section, create, read, update_frontmatter


def _decision(
    *,
    risk: Risk = Risk.MEDIUM,
    review_required: bool = True,
    complexity: Complexity = Complexity.MEDIUM,
) -> RoutingDecision:
    return RoutingDecision(
        risk=risk,
        complexity=complexity,
        architecture_impact=ArchitectureImpact.NONE,
        implementer="codex" if complexity is Complexity.HIGH else "cursor",
        plan_required=risk is not Risk.LOW,
        plan_approval_required=risk is Risk.HIGH,
        review_required=review_required,
        evidence_required=risk is Risk.HIGH,
        bypass_allowed=True,
        bypass_friction="none" if risk is Risk.LOW else "reason",
        reasons=[],
    )


def _gate(*, passed: bool) -> MergeGateResult:
    return MergeGateResult(
        passed=passed,
        previous_risk=Risk.MEDIUM,
        actual_risk=Risk.HIGH if not passed else Risk.MEDIUM,
        matched_rules=[],
        reason=None if passed else "BLOCKED — reroute to TRIAGE",
    )


def test_state_enum_has_exactly_ten_values() -> None:
    names = {member.name for member in State}
    assert names == {
        "BACKLOG",
        "TRIAGE",
        "PLAN_APPROVAL",
        "IMPLEMENTING",
        "REVIEW",
        "REWORK",
        "READY_TO_MERGE",
        "BLOCKED",
        "MERGED",
        "CANCELLED",
    }
    assert len(State) == 10


def test_terminal_states_have_no_table_exits() -> None:
    for state in (State.MERGED, State.CANCELLED):
        exits = [pair for pair in TRANSITIONS if pair[0] is state]
        assert exits == []


@pytest.mark.parametrize(
    ("current", "trigger", "dest"),
    [(state, trigger, dest) for (state, trigger), dest in TRANSITIONS.items()],
    ids=[
        f"{state.value}-{trigger.value}-{dest.value}"
        for (state, trigger), dest in TRANSITIONS.items()
    ],
)
def test_table_transition(current: State, trigger: Trigger, dest: State) -> None:
    result = transition(current, trigger)
    assert result.from_state is current
    assert result.to_state is dest
    assert result.trigger is trigger


def test_invalid_combination_raises() -> None:
    with pytest.raises(InvalidTransition, match="cannot apply 'merged' from TRIAGE"):
        transition(State.TRIAGE, Trigger.MERGED)


@pytest.mark.parametrize("trigger", list(Trigger), ids=lambda t: t.value)
def test_merged_rejects_all_triggers(trigger: Trigger) -> None:
    with pytest.raises(InvalidTransition):
        transition(State.MERGED, trigger)


@pytest.mark.parametrize("trigger", list(Trigger), ids=lambda t: t.value)
def test_cancelled_rejects_all_triggers(trigger: Trigger) -> None:
    with pytest.raises(InvalidTransition):
        transition(State.CANCELLED, trigger)


def test_start_epic_high_goes_to_plan_approval() -> None:
    decision = _decision(risk=Risk.HIGH)
    assert initial_state(decision, True) is State.PLAN_APPROVAL
    result = transition(
        State.BACKLOG,
        Trigger.START,
        decision,
        has_epic_decision=True,
    )
    assert result.to_state is State.PLAN_APPROVAL


def test_start_epic_medium_goes_to_implementing() -> None:
    decision = _decision(risk=Risk.MEDIUM)
    assert initial_state(decision, True) is State.IMPLEMENTING
    result = transition(
        State.BACKLOG,
        Trigger.START,
        decision,
        has_epic_decision=True,
    )
    assert result.to_state is State.IMPLEMENTING


def test_start_without_epic_goes_to_triage() -> None:
    decision = _decision(risk=Risk.MEDIUM)
    assert initial_state(decision, False) is State.TRIAGE
    result = transition(
        State.BACKLOG,
        Trigger.START,
        decision,
        has_epic_decision=False,
    )
    assert result.to_state is State.TRIAGE


def test_implement_done_without_review_goes_to_ready() -> None:
    result = transition(
        State.IMPLEMENTING,
        Trigger.IMPLEMENT_DONE,
        _decision(review_required=False),
    )
    assert result.to_state is State.READY_TO_MERGE


def test_implement_done_with_review_goes_to_review() -> None:
    result = transition(
        State.IMPLEMENTING,
        Trigger.IMPLEMENT_DONE,
        _decision(review_required=True),
    )
    assert result.to_state is State.REVIEW


def test_blocked_returns_to_previous_state(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="REVIEW")
    blocked = transition(State.REVIEW, Trigger.AGENT_BLOCKED)
    assert blocked.to_state is State.BLOCKED
    update_frontmatter(
        path,
        state=blocked.to_state.value,
        blocked_from=blocked.from_state.value,
    )
    tf = read(path)
    assert tf.frontmatter.state == "BLOCKED"
    assert tf.frontmatter.blocked_from == "REVIEW"
    resolved = transition(
        State.BLOCKED,
        Trigger.HUMAN_RESOLVED,
        blocked_from=State(tf.frontmatter.blocked_from),
    )
    assert resolved.to_state is State.REVIEW


def test_review_blocking_after_one_round_goes_to_rework(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="REVIEW")
    append_section(path, "Review — round 1", "findings")
    tf = read(path)
    assert review_cycle_count(tf) == 1
    result = transition(
        State.REVIEW,
        Trigger.REVIEW_BLOCKING,
        review_cycles=review_cycle_count(tf),
    )
    assert result.to_state is State.REWORK


def test_review_blocking_after_two_rounds_goes_to_blocked(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="REVIEW")
    append_section(path, "Review — round 1", "first")
    append_section(path, "Review — round 2", "second")
    tf = read(path)
    assert review_cycle_count(tf) == 2
    result = transition(
        State.REVIEW,
        Trigger.REVIEW_BLOCKING,
        review_cycles=review_cycle_count(tf),
    )
    assert result.to_state is State.BLOCKED
    assert result.reason == "review cycle limit reached (2)"


def test_ready_to_merge_all_clear(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="READY_TO_MERGE")
    tf = append_section(
        path,
        "Doc impact",
        "status: none\nfiles: []\n",
    )
    result = check_ready_to_merge(
        tf,
        _decision(),
        True,
        0,
        True,
        _gate(passed=True),
    )
    assert result.ready is True
    assert result.blockers == []


def test_ready_to_merge_missing_doc_impact(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    tf = create(path, 1, "t", state="READY_TO_MERGE")
    result = check_ready_to_merge(
        tf,
        _decision(),
        True,
        0,
        True,
        _gate(passed=True),
    )
    assert result.ready is False
    assert any("doc impact" in item for item in result.blockers)


def test_ready_to_merge_gate_failed(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="READY_TO_MERGE")
    tf = append_section(
        path,
        "Doc impact",
        "status: none\nfiles: []\n",
    )
    result = check_ready_to_merge(
        tf,
        _decision(),
        True,
        0,
        True,
        _gate(passed=False),
    )
    assert result.ready is False
    assert any(
        "merge gate" in item.lower() or "BLOCKED" in item for item in result.blockers
    )


def test_ready_to_merge_collects_all_blockers(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    tf = create(path, 1, "t", state="READY_TO_MERGE")
    result = check_ready_to_merge(
        tf,
        _decision(),
        False,
        2,
        False,
        _gate(passed=False),
    )
    assert result.ready is False
    assert len(result.blockers) == 5
    joined = " ".join(result.blockers)
    assert "verify" in joined
    assert "blocking findings" in joined
    assert "rebase" in joined
    assert "doc impact" in joined
    assert "BLOCKED" in joined or "merge gate" in joined.lower()
