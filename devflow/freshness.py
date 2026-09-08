"""Code freshness versus decision validity.

These are different questions:

Code freshness: is the reviewed *code* version still the one under review?
Input: head SHA + base SHA. Scope: source files, excluding `.devflow/tasks/**`.
Writing review results into the task file creates a new commit; counting that
file would invalidate the review in a loop.

Decision validity: are the *task records* still a sound merge decision?
Input: risk inputs, waivers, findings, approvals. Scope: the task file
itself, which is never excluded. A waiver reason can change the merge
decision without any code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from devflow.gitops import git_output

if TYPE_CHECKING:
    from devflow.taskfile import TaskFile

_TASK_PREFIX = ".devflow/tasks/"
_REVIEWED_STATES = frozenset({"REVIEW", "REWORK"})
_WAIVER_HEADING = "Waivers"
_HUMAN_HEADING = "Human decision"
_HEADING_RE = re.compile(r"^## ([^#].*?)\s*$", re.MULTILINE)
_REVIEW_ROUND_RE = re.compile(r"^Review — round \d+$")
_UNVERIFIED_ISSUE_NEEDLE = "unverified HIGH findings without human decision"


@dataclass
class ReviewRecord:
    head_sha: str
    base_sha: str
    round: int
    blocking_findings: int
    unverified_high: int
    timestamp: str


@dataclass
class FreshnessResult:
    fresh: bool
    reason: str
    changed_since_review: list[str]


@dataclass
class DecisionValidity:
    valid: bool
    issues: list[str]


@dataclass
class Waiver:
    id: str
    reason: str


def check_code_freshness(
    worktree: Path,
    record: ReviewRecord,
    current_head: str,
    current_base: str,
) -> FreshnessResult:
    if current_base != record.base_sha:
        return FreshnessResult(
            fresh=False,
            reason=f"base moved from {record.base_sha} to {current_base}",
            changed_since_review=[],
        )
    if current_head == record.head_sha:
        return FreshnessResult(fresh=True, reason="fresh", changed_since_review=[])

    changed = [
        path
        for path in _diff_names(worktree, record.head_sha, current_head)
        if not _is_task_path(path)
    ]
    if not changed:
        return FreshnessResult(fresh=True, reason="fresh", changed_since_review=[])
    return FreshnessResult(
        fresh=False,
        reason=f"{len(changed)} files changed since review",
        changed_since_review=changed,
    )


def check_decision_validity(tf: TaskFile) -> DecisionValidity:
    from devflow.taskfile import read_doc_impact

    issues: list[str] = []
    try:
        waivers = parse_waivers(tf)
    except ValueError:
        issues.append("waivers section is invalid")
        waivers = []
    for waiver in waivers:
        if not waiver.reason.strip():
            issues.append(f"waiver for {waiver.id} has no reason")
    try:
        impact = read_doc_impact(tf)
    except ValueError:
        issues.append("doc impact section is missing or invalid")
    else:
        if impact is None:
            issues.append("doc impact section is missing or invalid")
    records = tf.frontmatter.review_records
    reviewed = tf.frontmatter.state in _REVIEWED_STATES or _review_round_count(tf) > 0
    if reviewed and not records:
        issues.append("review was entered but review_records is empty")
    latest = latest_review_record(tf)
    if latest is not None and latest.unverified_high > 0 and not has_human_decision(tf):
        issues.append(
            f"{latest.unverified_high} unverified HIGH findings without human decision"
        )
    return DecisionValidity(valid=not issues, issues=issues)


def latest_review_record(tf: TaskFile) -> ReviewRecord | None:
    records = tf.frontmatter.review_records
    if not records:
        return None
    return max(records, key=lambda item: item.round)


def has_human_decision(tf: TaskFile) -> bool:
    try:
        waivers = parse_waivers(tf)
    except ValueError:
        waivers = []
    if any(waiver.reason.strip() for waiver in waivers):
        return True
    section = _section_content(tf.body, _HUMAN_HEADING)
    return bool(section and section.strip())


def decision_validity_blockers(validity: DecisionValidity) -> list[str]:
    return [issue for issue in validity.issues if _UNVERIFIED_ISSUE_NEEDLE not in issue]


def parse_waivers(tf: TaskFile) -> list[Waiver]:
    section = _section_content(tf.body, _WAIVER_HEADING)
    if section is None or not section.strip():
        return []
    raw = yaml.safe_load(section)
    items: list[object]
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        raise ValueError("Waivers section must be a YAML list")
    waivers: list[Waiver] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each waiver must be a mapping")
        ident = str(item.get("id") or item.get("finding") or "")
        reason_raw = item.get("reason")
        reason = "" if reason_raw is None else str(reason_raw)
        waivers.append(Waiver(id=ident or "(unknown)", reason=reason))
    return waivers


def _diff_names(worktree: Path, old: str, new: str) -> list[str]:
    output = git_output(worktree, "diff", "--name-only", old, new)
    return [line.replace("\\", "/") for line in output.splitlines() if line]


def _is_task_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == ".devflow/tasks" or normalized.startswith(_TASK_PREFIX)


def _review_round_count(tf: TaskFile) -> int:
    from devflow.taskfile import body_sections

    return sum(1 for heading in body_sections(tf) if _REVIEW_ROUND_RE.match(heading))


def _section_content(body: str, heading: str) -> str | None:
    matches = list(_HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1).strip() != heading:
            continue
        start = match.end()
        if start < len(body) and body[start] == "\n":
            start += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end]
    return None
