from __future__ import annotations

import copy
from pathlib import Path

from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    EpicProposal,
    PolicyResult,
    Risk,
    RoutingDecision,
    TriageSignals,
    apply_floor,
    check_merge_gate,
    decide,
    fast_lane_eligible,
    load_policy,
    needed_triage_fields,
    risk_from_signals,
    validate_policy,
)

_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "templates" / "project" / ".ai" / "policy.yml"
)
_TOP_LEVEL = ("floor", "signal_floor", "routing")


def _policy() -> dict[str, object]:
    return load_policy(_POLICY_PATH)


def _quiet() -> TriageSignals:
    return TriageSignals(
        transaction_change=False,
        concurrency_sensitive=False,
        architecture_boundary_change=False,
        unfamiliar_area=False,
    )


def test_policy_yml_is_valid_yaml_with_required_keys() -> None:
    data = _policy()
    for key in _TOP_LEVEL:
        assert key in data


def test_floor_auth_login_is_high() -> None:
    result = apply_floor(["src/auth/login.py"], [], _policy())
    assert result.risk_floor is Risk.HIGH
    assert result.matched_rules


def test_floor_oauth_wide_glob_is_high() -> None:
    result = apply_floor(["app/security/oauth.py"], [], _policy())
    assert result.risk_floor is Risk.HIGH


def test_floor_css_is_low() -> None:
    result = apply_floor(["styles/main.css"], [], _policy())
    assert result.risk_floor is Risk.LOW


def test_floor_orders_has_no_match() -> None:
    result = apply_floor(["src/orders/service.py"], [], _policy())
    assert result.risk_floor is None
    assert result.matched_rules == []


def test_floor_auth_and_css_high_wins() -> None:
    result = apply_floor(["src/auth/login.py", "styles/main.css"], [], _policy())
    assert result.risk_floor is Risk.HIGH


def _decision(
    policy: dict[str, object],
    *,
    floor: PolicyResult,
    signals: TriageSignals | None = None,
    complexity: Complexity | None = None,
    architecture_impact: ArchitectureImpact = ArchitectureImpact.NONE,
    uncertain: bool = False,
    user_risk_hint: Risk | None = None,
    paths: list[str] | None = None,
    epic: EpicProposal | None = None,
) -> RoutingDecision:
    return decide(
        floor,
        signals if signals is not None else _quiet(),
        complexity,
        architecture_impact,
        uncertain,
        user_risk_hint,
        paths or ["src/orders/service.py"],
        policy,
        epic,
    )


def test_signal_transaction_change_is_high() -> None:
    signals = _quiet()
    signals.transaction_change = True
    assert risk_from_signals(signals, _policy()) is Risk.HIGH


def test_signal_unfamiliar_area_is_medium() -> None:
    signals = _quiet()
    signals.unfamiliar_area = True
    assert risk_from_signals(signals, _policy()) is Risk.MEDIUM


def test_signal_none_is_none() -> None:
    assert risk_from_signals(_quiet(), _policy()) is None


def test_bypass_high_allows_with_reason_friction() -> None:
    decision = _decision(_policy(), floor=PolicyResult(Risk.HIGH, None, False, []))
    assert decision.bypass_allowed is True
    assert decision.bypass_friction == "reason"


def test_bypass_medium_allows_with_reason_friction() -> None:
    decision = _decision(_policy(), floor=PolicyResult(Risk.MEDIUM, None, False, []))
    assert decision.bypass_allowed is True
    assert decision.bypass_friction == "reason"


def test_bypass_low_has_none_friction() -> None:
    decision = _decision(_policy(), floor=PolicyResult(Risk.LOW, None, False, []))
    assert decision.bypass_allowed is True
    assert decision.bypass_friction == "none"


def test_bypass_allowed_never_false() -> None:
    for risk in (Risk.LOW, Risk.MEDIUM, Risk.HIGH):
        decision = _decision(
            _policy(),
            floor=PolicyResult(risk, None, False, []),
            paths=["src/auth/login.py"],
        )
        assert decision.bypass_allowed is True


def test_decide_user_hint_cannot_lower_floor() -> None:
    policy = _policy()
    floor = apply_floor(["src/auth/login.py"], [], policy)
    decision = decide(
        floor,
        _quiet(),
        None,
        ArchitectureImpact.NONE,
        False,
        Risk.LOW,
        ["src/auth/login.py"],
        policy,
    )
    assert decision.risk is Risk.HIGH


def test_decide_user_hint_raises_when_no_floor() -> None:
    policy = _policy()
    floor = apply_floor(["src/orders/service.py"], [], policy)
    decision = decide(
        floor,
        _quiet(),
        None,
        ArchitectureImpact.NONE,
        False,
        Risk.HIGH,
        ["src/orders/service.py"],
        policy,
    )
    assert decision.risk is Risk.HIGH


def test_decide_uncertain_raises_medium_to_high() -> None:
    policy = _policy()
    floor = PolicyResult(Risk.MEDIUM, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.MEDIUM,
        ArchitectureImpact.NONE,
        True,
        None,
        ["src/orders/service.py"],
        policy,
    )
    assert decision.risk is Risk.HIGH


def test_decide_uncertain_does_not_exceed_high() -> None:
    policy = _policy()
    floor = PolicyResult(Risk.HIGH, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.HIGH,
        ArchitectureImpact.NONE,
        True,
        None,
        ["src/auth/login.py"],
        policy,
    )
    assert decision.risk is Risk.HIGH
    assert decision.complexity is Complexity.HIGH


def test_decide_high_complexity_uses_codex() -> None:
    policy = _policy()
    floor = PolicyResult(None, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.HIGH,
        ArchitectureImpact.NONE,
        False,
        None,
        ["src/orders/service.py"],
        policy,
    )
    assert decision.implementer == "codex"


def test_decide_medium_complexity_uses_cursor() -> None:
    policy = _policy()
    floor = PolicyResult(None, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.MEDIUM,
        ArchitectureImpact.NONE,
        False,
        None,
        ["src/orders/service.py"],
        policy,
    )
    assert decision.implementer == "cursor"


def test_decide_low_risk_skips_review() -> None:
    policy = _policy()
    floor = PolicyResult(Risk.LOW, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.LOW,
        ArchitectureImpact.NONE,
        False,
        None,
        ["styles/main.css"],
        policy,
    )
    assert decision.review_required is False


def test_decide_high_risk_requires_evidence_and_plan_approval() -> None:
    policy = _policy()
    floor = PolicyResult(Risk.HIGH, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.MEDIUM,
        ArchitectureImpact.NONE,
        False,
        None,
        ["src/auth/login.py"],
        policy,
    )
    assert decision.evidence_required is True
    assert decision.plan_approval_required is True


def test_decide_architecture_yes_adds_block_reason() -> None:
    policy = _policy()
    floor = PolicyResult(None, None, False, [])
    decision = decide(
        floor,
        _quiet(),
        Complexity.MEDIUM,
        ArchitectureImpact.YES,
        False,
        None,
        ["src/orders/service.py"],
        policy,
    )
    assert "architecture block" in decision.reasons


def test_needed_triage_fields_when_floor_has_only_risk() -> None:
    floor = PolicyResult(
        Risk.HIGH, None, False, ["**/*auth* (path: src/auth/login.py)"]
    )
    fields = needed_triage_fields(floor, None)
    assert "complexity" in fields
    assert "architecture_impact" in fields


def test_needed_triage_fields_skips_complexity_when_floor_has_it() -> None:
    floor = PolicyResult(Risk.HIGH, Complexity.MEDIUM, False, [])
    fields = needed_triage_fields(floor, None)
    assert "complexity" not in fields


def test_needed_triage_fields_full_epic_skips_triage() -> None:
    floor = PolicyResult(None, None, False, [])
    epic = EpicProposal(Risk.MEDIUM, Complexity.MEDIUM, "from epic")
    assert needed_triage_fields(floor, epic) == []


def test_needed_triage_fields_epic_risk_only_asks_complexity() -> None:
    floor = PolicyResult(None, None, False, [])
    epic = EpicProposal(Risk.HIGH, None, "from epic")
    fields = needed_triage_fields(floor, epic)
    assert fields == ["complexity"]


def test_validate_policy_ignores_no_bypass_key() -> None:
    policy = copy.deepcopy(_policy())
    policy["reviewed_for_this_project"] = True
    policy["no_bypass"] = ["**/not-in-floor/**"]
    assert validate_policy(policy) == []


def test_validate_policy_requires_three_top_level_keys() -> None:
    errors = validate_policy({"reviewed_for_this_project": True})
    joined = " ".join(errors)
    assert "floor" in joined
    assert "signal_floor" in joined
    assert "routing" in joined


def test_validate_policy_reviewed_flag() -> None:
    policy = copy.deepcopy(_policy())
    policy["reviewed_for_this_project"] = False
    errors = validate_policy(policy)
    assert any("template defaults" in item for item in errors)
    policy["reviewed_for_this_project"] = True
    assert validate_policy(policy) == []


def test_epic_high_without_floor() -> None:
    policy = _policy()
    floor = apply_floor(["src/orders/service.py"], [], policy)
    epic = EpicProposal(Risk.HIGH, Complexity.MEDIUM, "epic")
    decision = _decision(policy, floor=floor, epic=epic)
    assert decision.risk is Risk.HIGH
    assert any("from epic proposal" in item for item in decision.reasons)


def test_epic_low_cannot_lower_floor_high() -> None:
    policy = _policy()
    floor = apply_floor(["src/auth/login.py"], [], policy)
    epic = EpicProposal(Risk.LOW, Complexity.LOW, "epic")
    decision = _decision(policy, floor=floor, paths=["src/auth/login.py"], epic=epic)
    assert decision.risk is Risk.HIGH


def test_epic_medium_plus_transaction_signal_is_high() -> None:
    policy = _policy()
    floor = apply_floor(["src/orders/service.py"], [], policy)
    signals = _quiet()
    signals.transaction_change = True
    epic = EpicProposal(Risk.MEDIUM, Complexity.MEDIUM, "epic")
    decision = _decision(policy, floor=floor, signals=signals, epic=epic)
    assert decision.risk is Risk.HIGH


def test_epic_high_complexity_uses_codex() -> None:
    policy = _policy()
    floor = PolicyResult(None, None, False, [])
    epic = EpicProposal(Risk.LOW, Complexity.HIGH, "epic")
    decision = _decision(policy, floor=floor, epic=epic)
    assert decision.implementer == "codex"
    assert any(
        "complexity HIGH from epic proposal" in item for item in decision.reasons
    )


def test_check_merge_equal_risk_passes() -> None:
    policy = _policy()
    previous = _decision(policy, floor=PolicyResult(Risk.HIGH, None, False, []))
    result = check_merge_gate(previous, ["src/auth/login.py"], policy)
    assert result.passed is True
    assert result.actual_risk is Risk.HIGH


def test_check_merge_lower_actual_passes() -> None:
    policy = _policy()
    previous = _decision(policy, floor=PolicyResult(Risk.HIGH, None, False, []))
    result = check_merge_gate(previous, ["styles/main.css"], policy)
    assert result.passed is True
    assert result.actual_risk is Risk.LOW


def test_check_merge_higher_actual_blocks() -> None:
    policy = _policy()
    previous = _decision(policy, floor=PolicyResult(Risk.MEDIUM, None, False, []))
    result = check_merge_gate(previous, ["src/auth/login.py"], policy)
    assert result.passed is False
    assert result.actual_risk is Risk.HIGH
    assert result.matched_rules


def test_check_merge_unexpected_file_without_floor_match_passes() -> None:
    policy = _policy()
    previous = _decision(policy, floor=PolicyResult(Risk.MEDIUM, None, False, []))
    result = check_merge_gate(previous, ["src/orders/unexpected.py"], policy)
    assert result.passed is True


def test_fast_lane_markdown_only() -> None:
    ok, reason = fast_lane_eligible(["README.md", "notes.md"], _policy())
    assert ok is True
    assert reason == "all paths fast-lane eligible"


def test_fast_lane_css_only() -> None:
    ok, reason = fast_lane_eligible(["styles/main.css"], _policy())
    assert ok is True
    assert reason == "all paths fast-lane eligible"


def test_fast_lane_auth_css_conflict_is_not_eligible() -> None:
    ok, reason = fast_lane_eligible(["src/auth/login.css"], _policy())
    assert ok is False
    assert "src/auth/login.css" in reason
    assert "**/*auth*" in reason or "**/*login*" in reason
    assert "HIGH" in reason


def test_fast_lane_adr_is_not_eligible() -> None:
    ok, reason = fast_lane_eligible(["docs/adr/ADR-014.md"], _policy())
    assert ok is False
    assert "docs/adr" in reason


def test_fast_lane_unmatched_path() -> None:
    ok, reason = fast_lane_eligible(["src/orders/service.py"], _policy())
    assert ok is False
    assert reason == "src/orders/service.py: unmatched path"


def test_fast_lane_markdown_and_css_mix() -> None:
    ok, reason = fast_lane_eligible(["README.md", "styles/main.css"], _policy())
    assert ok is True
    assert reason == "all paths fast-lane eligible"


def test_fast_lane_one_unmatched_file_disqualifies() -> None:
    ok, reason = fast_lane_eligible(["README.md", "src/orders/service.py"], _policy())
    assert ok is False
    assert "src/orders/service.py" in reason


def test_fast_lane_empty_paths_not_eligible() -> None:
    ok, reason = fast_lane_eligible([], _policy())
    assert ok is False
    assert reason


def test_validate_policy_fast_lane_requires_low_risk() -> None:
    policy = copy.deepcopy(_policy())
    policy["reviewed_for_this_project"] = True
    policy["floor"]["high"][0]["fast_lane"] = True
    errors = validate_policy(policy)
    assert any("fast_lane" in item and "LOW" in item for item in errors)
