from __future__ import annotations

import enum
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

_REQUIRED_KEYS = ("floor", "signal_floor", "routing")
_KNOWN_SIGNALS = (
    "transaction_change",
    "concurrency_sensitive",
    "architecture_boundary_change",
    "unfamiliar_area",
)
_LEVELS = ("LOW", "MEDIUM", "HIGH")
_REVIEWED_ERROR = (
    "policy.yml still has template defaults.\n"
    "Review floor globs for this project, then set\n"
    "reviewed_for_this_project: true"
)

EnumT = TypeVar("EnumT", bound=enum.Enum)
BypassFriction = Literal["none", "reason"]
PlanDetail = Literal["none", "brief", "formal"]


class Risk(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Complexity(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ArchitectureImpact(enum.StrEnum):
    NONE = "NONE"
    POSSIBLE = "POSSIBLE"
    YES = "YES"


_RISK_RANK = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2}
_COMPLEXITY_RANK = {Complexity.LOW: 0, Complexity.MEDIUM: 1, Complexity.HIGH: 2}


@dataclass
class PolicyResult:
    risk_floor: Risk | None
    complexity_hint: Complexity | None
    architecture_block: bool
    matched_rules: list[str] = field(default_factory=list)


@dataclass
class TriageSignals:
    transaction_change: bool
    concurrency_sensitive: bool
    architecture_boundary_change: bool
    unfamiliar_area: bool


@dataclass
class EpicProposal:
    risk: Risk | None
    complexity: Complexity | None
    reason: str | None


@dataclass
class RoutingDecision:
    risk: Risk
    complexity: Complexity
    architecture_impact: ArchitectureImpact
    implementer: str
    plan_required: bool
    plan_approval_required: bool
    plan_detail: PlanDetail
    review_required: bool
    evidence_required: bool
    bypass_allowed: bool
    bypass_friction: BypassFriction
    reasons: list[str]


@dataclass
class MergeGateResult:
    passed: bool
    previous_risk: Risk
    actual_risk: Risk
    matched_rules: list[str]
    reason: str | None


def _parse_enum(enum_cls: type[EnumT], value: object) -> EnumT:
    text = str(value).strip().upper()
    try:
        return enum_cls(text)
    except ValueError as exc:
        raise ValueError(f"invalid {enum_cls.__name__}: {value!r}") from exc


def _posix(value: str) -> str:
    return value.replace("\\", "/")


def _glob_match(value: str, pattern: str) -> bool:
    value = _posix(value)
    pattern = _posix(pattern)
    if fnmatch.fnmatch(value, pattern):
        return True
    # `**/*.md` should match a root-level `notes.md`, as in gitignore globs.
    if pattern.startswith("**/") and fnmatch.fnmatch(value, pattern[3:]):
        return True
    return False


def _max_risk(*values: Risk | None) -> Risk | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present, key=lambda item: _RISK_RANK[item])


def _bump_risk(value: Risk) -> Risk:
    if value is Risk.LOW:
        return Risk.MEDIUM
    return Risk.HIGH


def _bump_complexity(value: Complexity) -> Complexity:
    if value is Complexity.LOW:
        return Complexity.MEDIUM
    return Complexity.HIGH


def _rule_glob(matched: str) -> str:
    return matched.split(" (path:", 1)[0].split(" (label:", 1)[0]


def _friction(risk: Risk) -> BypassFriction:
    return "none" if risk is Risk.LOW else "reason"


def plan_detail_for(complexity: Complexity) -> PlanDetail:
    if complexity is Complexity.LOW:
        return "none"
    if complexity is Complexity.MEDIUM:
        return "brief"
    return "formal"


def _rule_patterns(rule: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    raw_paths = rule.get("paths")
    if isinstance(raw_paths, list):
        patterns.extend(str(item) for item in raw_paths if item)
    elif isinstance(raw_paths, str) and raw_paths.strip():
        patterns.append(raw_paths)
    single = rule.get("path")
    if isinstance(single, str) and single.strip():
        patterns.append(single)
    return patterns


def _floor_rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    floor = policy.get("floor")
    rules: list[dict[str, Any]] = []
    if isinstance(floor, dict):
        for group in floor.values():
            if isinstance(group, list):
                rules.extend(item for item in group if isinstance(item, dict))
    elif isinstance(floor, list):
        rules.extend(item for item in floor if isinstance(item, dict))
    return rules


def load_policy(path: Path) -> dict[str, Any]:
    """Load policy.yml from a working-tree path.

    Use this for interactive commands such as `devflow classify`. Merge
    checks and the runner must call load_policy_from_base() so a branch
    cannot weaken the rules that review it.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy.yml must be a mapping")
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise ValueError(f"policy.yml missing keys: {', '.join(missing)}")
    return raw


def apply_floor(
    paths: list[str], labels: list[str], policy: dict[str, Any]
) -> PolicyResult:
    matched_rules: list[str] = []
    risks: list[Risk] = []
    complexities: list[Complexity] = []
    architecture_block = False

    for rule in _floor_rules(policy):
        patterns = _rule_patterns(rule)
        rule_label = str(rule.get("label", ""))
        hits: list[str] = []
        for path in paths:
            for pattern in patterns:
                if pattern and _glob_match(path, pattern):
                    hits.append(f"{pattern} (path: {path})")
        for label in labels:
            for pattern in patterns:
                if pattern and _glob_match(label, pattern):
                    hits.append(f"{pattern} (label: {label})")
            if rule_label and label.casefold() == rule_label.casefold():
                hits.append(f"{rule_label} (label: {label})")
        if not hits:
            continue
        matched_rules.extend(hits)
        if "risk" in rule:
            risks.append(_parse_enum(Risk, rule["risk"]))
        if "complexity" in rule:
            complexities.append(_parse_enum(Complexity, rule["complexity"]))
        if rule.get("architecture_block"):
            architecture_block = True

    risk_floor = _max_risk(*risks)
    complexity_hint = None
    if complexities:
        complexity_hint = max(complexities, key=lambda item: _COMPLEXITY_RANK[item])
    return PolicyResult(
        risk_floor=risk_floor,
        complexity_hint=complexity_hint,
        architecture_block=architecture_block,
        matched_rules=matched_rules,
    )


def risk_from_signals(signals: TriageSignals, policy: dict[str, Any]) -> Risk | None:
    mapping = policy.get("signal_floor") or {}
    if not isinstance(mapping, dict):
        return None
    risks: list[Risk] = []
    for name in _KNOWN_SIGNALS:
        if getattr(signals, name) and name in mapping:
            risks.append(_parse_enum(Risk, mapping[name]))
    return _max_risk(*risks)


def needed_triage_fields(
    floor: PolicyResult,
    epic: EpicProposal | None,
) -> list[str]:
    if epic is not None and epic.risk is not None and epic.complexity is not None:
        return []
    if epic is not None:
        fields: list[str] = []
        if epic.complexity is None:
            fields.append("complexity")
        if epic.risk is None:
            fields.append("risk")
        return fields
    fields = []
    if floor.complexity_hint is None:
        fields.append("complexity")
    if not floor.architecture_block:
        fields.append("architecture_impact")
    fields.append("signals")
    return fields


def decide(
    floor: PolicyResult,
    signals: TriageSignals | None,
    complexity: Complexity | None,
    architecture_impact: ArchitectureImpact,
    uncertain: bool,
    user_risk_hint: Risk | None,
    paths: list[str],
    policy: dict[str, Any],
    epic: EpicProposal | None = None,
) -> RoutingDecision:
    _ = paths
    signal_risk = risk_from_signals(signals, policy) if signals is not None else None
    epic_risk = epic.risk if epic is not None else None
    risk = (
        _max_risk(floor.risk_floor, signal_risk, epic_risk, user_risk_hint) or Risk.LOW
    )
    if epic is not None and epic.complexity is not None:
        chosen_complexity = epic.complexity
    else:
        chosen_complexity = floor.complexity_hint or complexity or Complexity.MEDIUM
    reasons: list[str] = []

    if floor.risk_floor is not None:
        glob = _rule_glob(floor.matched_rules[0]) if floor.matched_rules else ""
        extra = f" ({glob})" if glob else ""
        reasons.append(f"risk {floor.risk_floor.value} from floor{extra}")
    if epic is not None and epic.risk is not None:
        reasons.append(f"risk {epic.risk.value} from epic proposal")
    if user_risk_hint is not None:
        reasons.append(f"risk {user_risk_hint.value} from user hint")
    if signals is not None:
        mapping = policy.get("signal_floor") or {}
        for name in _KNOWN_SIGNALS:
            if getattr(signals, name) and name in mapping:
                reasons.append(f"signal: {name} → {mapping[name]}")
    if epic is not None and epic.complexity is not None:
        reasons.append(f"complexity {epic.complexity.value} from epic proposal")

    if uncertain:
        raised_risk = _bump_risk(risk)
        raised_complexity = _bump_complexity(chosen_complexity)
        if raised_risk is not risk:
            reasons.append(f"uncertain: raised risk {risk.value} → {raised_risk.value}")
        if raised_complexity is not chosen_complexity:
            reasons.append(
                "uncertain: raised complexity "
                f"{chosen_complexity.value} → {raised_complexity.value}"
            )
        risk = raised_risk
        chosen_complexity = raised_complexity

    if floor.risk_floor is not None:
        risk = _max_risk(risk, floor.risk_floor) or risk

    routing = policy.get("routing") or {}
    implementer = str(
        routing.get("complexity", {}).get(chosen_complexity.value, "cursor")
    )
    risk_table = routing.get("risk") if isinstance(routing, dict) else None
    risk_route: dict[str, Any] = {}
    if isinstance(risk_table, dict):
        raw_route = risk_table.get(risk.value, {})
        if isinstance(raw_route, dict):
            risk_route = raw_route
    detail = plan_detail_for(chosen_complexity)
    plan_required = detail != "none"
    plan_approval_required = risk_route.get("plan_approval") is True
    if architecture_impact in {ArchitectureImpact.POSSIBLE, ArchitectureImpact.YES}:
        plan_approval_required = True
    review_required = risk_route.get("review") is True
    evidence_required = risk_route.get("evidence") is True

    if architecture_impact in {ArchitectureImpact.POSSIBLE, ArchitectureImpact.YES}:
        reasons.append("architecture block")

    return RoutingDecision(
        risk=risk,
        complexity=chosen_complexity,
        architecture_impact=architecture_impact,
        implementer=implementer,
        plan_required=plan_required,
        plan_approval_required=plan_approval_required,
        plan_detail=detail,
        review_required=review_required,
        evidence_required=evidence_required,
        bypass_allowed=True,
        bypass_friction=_friction(risk),
        reasons=reasons,
    )


def check_merge_gate(
    decision: RoutingDecision,
    actual_paths: list[str],
    policy: dict[str, Any],
) -> MergeGateResult:
    floor = apply_floor(actual_paths, [], policy)
    actual_risk = floor.risk_floor or Risk.LOW
    previous = decision.risk
    if _RISK_RANK[actual_risk] > _RISK_RANK[previous]:
        return MergeGateResult(
            passed=False,
            previous_risk=previous,
            actual_risk=actual_risk,
            matched_rules=floor.matched_rules,
            reason="BLOCKED — reroute to TRIAGE",
        )
    return MergeGateResult(
        passed=True,
        previous_risk=previous,
        actual_risk=actual_risk,
        matched_rules=floor.matched_rules,
        reason=None,
    )


def fast_lane_eligible(paths: list[str], policy: dict[str, Any]) -> tuple[bool, str]:
    if not paths:
        return False, "no paths given"
    for path in paths:
        blocking: list[tuple[str, Risk | None]] = []
        matched = False
        for rule in _floor_rules(policy):
            for pattern in _rule_patterns(rule):
                if not pattern or not _glob_match(path, pattern):
                    continue
                matched = True
                if rule.get("fast_lane") is True:
                    continue
                risk = None
                if "risk" in rule:
                    risk = _parse_enum(Risk, rule["risk"])
                blocking.append((pattern, risk))
        if not matched:
            return False, f"{path}: unmatched path"
        if blocking:
            pattern, risk = blocking[0]
            if risk is not None:
                return False, f"{path} matches {pattern} (risk {risk.value})"
            return False, f"{path} matches {pattern}"
    return True, "all paths fast-lane eligible"


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if policy.get("reviewed_for_this_project") is not True:
        errors.append(_REVIEWED_ERROR)

    missing = [key for key in _REQUIRED_KEYS if key not in policy]
    if missing:
        errors.append(f"policy.yml missing keys: {', '.join(missing)}")

    routing = policy.get("routing") or {}
    complexity_table = routing.get("complexity") if isinstance(routing, dict) else None
    risk_table = routing.get("risk") if isinstance(routing, dict) else None
    if not isinstance(complexity_table, dict):
        errors.append("routing table missing complexity levels")
    else:
        for level in _LEVELS:
            if level not in complexity_table:
                errors.append(f"routing.complexity missing {level}")
    if not isinstance(risk_table, dict):
        errors.append("routing table missing risk levels")
        risk_table = {}
    for level in _LEVELS:
        if level not in risk_table:
            errors.append(f"routing.risk missing {level}")

    signal_floor = policy.get("signal_floor") or {}
    if isinstance(signal_floor, dict):
        for name in signal_floor:
            if name not in _KNOWN_SIGNALS:
                errors.append(f"unknown signal in signal_floor: {name}")
    else:
        errors.append("signal_floor must be a mapping")

    for rule in _floor_rules(policy):
        if rule.get("fast_lane") is not True:
            continue
        risk_raw = rule.get("risk")
        if risk_raw is None or str(risk_raw).strip().upper() != Risk.LOW.value:
            patterns = ", ".join(_rule_patterns(rule)) or "(unnamed rule)"
            errors.append(f"fast_lane: true requires risk LOW ({patterns})")

    return errors
