"""Review finding parsing.

Severity is never lowered. Missing evidence marks verified=False.

Merge-gate behaviour:

- verified=True and BLOCKER/HIGH → merge blocked, rework triggered
- verified=False and BLOCKER/HIGH → rework is not triggered; merge stays
  closed until a human decision exists
- MEDIUM → fix or waive
- LOW → non-blocking
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

import yaml

Severity = Literal["BLOCKER", "HIGH", "MEDIUM", "LOW"]
_SEVERITIES = {"BLOCKER", "HIGH", "MEDIUM", "LOW"}
_FENCE_RE = re.compile(r"```(?:yaml)?\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class Finding:
    id: str
    severity: Severity
    verified: bool
    category: str
    location: str
    problem: str
    evidence: str | None
    blocking: bool


def parse_findings(review_output: str) -> list[Finding]:
    chunks = [match.group(1) for match in _FENCE_RE.finditer(review_output)]
    if not chunks:
        chunks = [review_output]
    findings: list[Finding] = []
    for chunk in chunks:
        for document in yaml.safe_load_all(chunk):
            findings.extend(_from_document(document))
    return findings


def _from_document(document: object) -> list[Finding]:
    if document is None:
        return []
    if isinstance(document, list):
        findings: list[Finding] = []
        for item in document:
            findings.extend(_from_document(item))
        return findings
    if not isinstance(document, dict):
        return []
    nested = document.get("findings")
    if isinstance(nested, list):
        return _from_document(nested)
    if "id" not in document:
        return []
    return [_one(document)]


def _one(raw: dict[str, Any]) -> Finding:
    severity_name = str(raw.get("severity", "")).strip().upper()
    if severity_name not in _SEVERITIES:
        raise ValueError(f"invalid finding severity: {raw.get('severity')!r}")
    severity = cast(Severity, severity_name)
    evidence_raw = raw.get("evidence")
    evidence = None if evidence_raw is None else str(evidence_raw).strip() or None
    verified = evidence is not None
    blocking = verified and severity in {"BLOCKER", "HIGH"}
    return Finding(
        id=str(raw.get("id", "")),
        severity=severity,
        verified=verified,
        category=str(raw.get("category", "")),
        location=str(raw.get("location", "")),
        problem=str(raw.get("problem", "")),
        evidence=evidence,
        blocking=blocking,
    )
