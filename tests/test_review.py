from __future__ import annotations

import inspect

from devflow.review import _one, parse_findings


def test_parse_findings_unverified_high_keeps_severity() -> None:
    findings = parse_findings(
        "id: F1\n"
        "severity: HIGH\n"
        "category: correctness\n"
        "location: src/app.py:1\n"
        "problem: possible bug\n"
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "HIGH"
    assert finding.verified is False
    assert finding.blocking is False
    assert finding.evidence is None


def test_parse_findings_verified_high() -> None:
    findings = parse_findings(
        "id: F1\n"
        "severity: HIGH\n"
        "category: correctness\n"
        "location: src/app.py:1\n"
        "problem: bug\n"
        "evidence: test\n"
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "HIGH"
    assert finding.verified is True
    assert finding.blocking is True
    assert finding.evidence == "test"


def test_parse_findings_medium_is_not_blocking() -> None:
    findings = parse_findings(
        "id: F2\n"
        "severity: MEDIUM\n"
        "category: style\n"
        "location: src/app.py:2\n"
        "problem: naming\n"
        "evidence: grep\n"
    )
    assert len(findings) == 1
    assert findings[0].severity == "MEDIUM"
    assert findings[0].blocking is False


def test_parse_findings_never_lowers_high_severity() -> None:
    payloads = [
        "id: F1\nseverity: HIGH\ncategory: c\nlocation: l\nproblem: p\n",
        "id: F1\nseverity: HIGH\ncategory: c\nlocation: l\nproblem: p\nevidence: ''\n",
        "id: F1\nseverity: HIGH\ncategory: c\nlocation: l\nproblem: p\nevidence:\n",
        (
            "id: F1\nseverity: high\ncategory: c\nlocation: l\n"
            "problem: p\nevidence: test\n"
        ),
        (
            "findings:\n"
            "  - id: F1\n"
            "    severity: HIGH\n"
            "    category: c\n"
            "    location: l\n"
            "    problem: p\n"
        ),
        "```yaml\nid: F1\nseverity: HIGH\ncategory: c\nlocation: l\nproblem: p\n```\n",
    ]
    for raw in payloads:
        findings = parse_findings(raw)
        assert findings, raw
        for finding in findings:
            assert finding.severity == "HIGH"


def test_parse_findings_source_never_downgrades_severity() -> None:
    source = inspect.getsource(_one)
    assert "MEDIUM" not in source
    assert "LOW" not in source
    assert "severity_name" in source
