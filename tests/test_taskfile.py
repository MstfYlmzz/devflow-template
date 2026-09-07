from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from devflow.policy import Complexity, Risk, TriageSignals
from devflow.taskfile import (
    TaskFrontmatter,
    append_section,
    body_sections,
    create,
    decision_inputs,
    read,
    read_doc_impact,
    update_frontmatter,
)


def _path(tmp_path: Path, name: str = "1.md") -> Path:
    return tmp_path / name


def test_read_types_fields(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.write_text(
        "---\n"
        "id: 184\n"
        "title: Order cancellation inventory release\n"
        "epic: order-approval\n"
        "state: REVIEW\n"
        "risk_proposed: MEDIUM\n"
        "risk_reason: inventory side effects\n"
        "complexity_proposed: MEDIUM\n"
        "modules:\n"
        "  - inventory\n"
        "blocked_by:\n"
        "  - 12\n"
        "adr:\n"
        "  - ADR-003\n"
        "floor_risk: null\n"
        "floor_matched: []\n"
        "signals:\n"
        "  transaction_change: true\n"
        "  concurrency_sensitive: false\n"
        "  architecture_boundary_change: false\n"
        "  unfamiliar_area: false\n"
        "architecture_impact: NONE\n"
        "uncertain: false\n"
        "floor_risk_actual: null\n"
        "floor_matched_actual: []\n"
        "---\n"
        "## Issue\n"
        "\n"
        "stock is wrong\n",
        encoding="utf-8",
    )
    tf = read(path)
    assert tf.frontmatter.id == 184
    assert tf.frontmatter.title == "Order cancellation inventory release"
    assert tf.frontmatter.epic == "order-approval"
    assert tf.frontmatter.state == "REVIEW"
    assert tf.frontmatter.risk_proposed is Risk.MEDIUM
    assert tf.frontmatter.complexity_proposed is Complexity.MEDIUM
    assert tf.frontmatter.modules == ["inventory"]
    assert tf.frontmatter.blocked_by == [12]
    assert tf.frontmatter.adr == ["ADR-003"]
    assert isinstance(tf.frontmatter.signals, TriageSignals)
    assert tf.frontmatter.signals.transaction_change is True
    assert tf.frontmatter.uncertain is False
    assert "stock is wrong" in tf.body


def test_read_missing_frontmatter(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.write_text("# no frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        read(path)


def test_read_unknown_key(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.write_text(
        "---\nid: 1\ntitle: t\nstate: BACKLOG\nrisk: MEDIUM\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown frontmatter key"):
        read(path)


def test_create_twice_raises(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "first")
    with pytest.raises(FileExistsError):
        create(path, 1, "second")


def test_update_frontmatter_does_not_change_body(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Issue", "hello")
    body = read(path).body
    updated = update_frontmatter(path, state="TRIAGE")
    assert updated.body == body
    assert updated.frontmatter.state == "TRIAGE"


def test_update_frontmatter_unknown_field(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    with pytest.raises(ValueError, match="unknown frontmatter field"):
        update_frontmatter(path, risk="MEDIUM")


def test_append_section_preserves_previous_text(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    first = append_section(path, "Issue", "the problem")
    before = first.body
    second = append_section(path, "Plan", "the plan")
    assert second.body.startswith(before)
    assert "the problem" in second.body
    assert body_sections(second) == ["Issue", "Plan"]


def test_append_section_keeps_order(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Issue", "a")
    append_section(path, "Plan", "b")
    tf = append_section(path, "Verify", "c")
    assert body_sections(tf) == ["Issue", "Plan", "Verify"]


def test_append_redacts_anthropic_key(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Notes", "token sk-ant-abc123xyz leftover")
    text = path.read_text(encoding="utf-8")
    assert "sk-ant-abc123xyz" not in text
    assert "[REDACTED]" in text


def test_append_redacts_password_assignment(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Notes", "PASSWORD=hunter2")
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "PASSWORD=[REDACTED]" in text


def test_append_leaves_normal_code(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    code = "def add(a, b):\n    return a + b\n"
    tf = append_section(path, "Code", code)
    assert "def add(a, b):" in tf.body
    assert "return a + b" in tf.body
    assert "[REDACTED]" not in tf.body


def test_update_frontmatter_redacts(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    update_frontmatter(path, risk_reason="leak sk-ant-abc123xyz here")
    text = path.read_text(encoding="utf-8")
    assert "sk-ant-abc123xyz" not in text
    assert "[REDACTED]" in text


def test_task_frontmatter_has_no_result_fields() -> None:
    names = {item.name for item in dataclasses.fields(TaskFrontmatter)}
    forbidden = {"risk", "complexity", "implementer", "review_required"}
    assert names.isdisjoint(forbidden)


def test_doc_impact_parses_all_statuses(tmp_path: Path) -> None:
    cases: list[tuple[str, str, list[str], str | None]] = [
        ("status: none\nfiles: []\n", "none", [], None),
        (
            "status: updated\nfiles:\n  - docs/architecture/backend.md\n",
            "updated",
            ["docs/architecture/backend.md"],
            None,
        ),
        (
            "status: adr_required\nfiles: []\nadr: ADR-004\n",
            "adr_required",
            [],
            "ADR-004",
        ),
    ]
    for index, (section, status, files, adr) in enumerate(cases):
        path = _path(tmp_path, f"{index}.md")
        create(path, index, "t")
        tf = append_section(path, "Doc impact", section)
        impact = read_doc_impact(tf)
        assert impact is not None
        assert impact.status == status
        assert impact.files == files
        assert impact.adr == adr


def test_doc_impact_invalid_status(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    tf = append_section(path, "Doc impact", "status: follow-up\n")
    with pytest.raises(ValueError, match="invalid Doc impact status"):
        read_doc_impact(tf)


def test_doc_impact_missing_section(tmp_path: Path) -> None:
    path = _path(tmp_path)
    tf = create(path, 1, "t")
    assert read_doc_impact(tf) is None


def test_decision_inputs_with_epic_proposal(tmp_path: Path) -> None:
    path = _path(tmp_path)
    tf = create(
        path,
        184,
        "Order cancellation inventory release",
        "order-approval",
        risk_proposed=Risk.MEDIUM,
        complexity_proposed=Complexity.MEDIUM,
    )
    epic, floor, signals, complexity, architecture_impact, uncertain = decision_inputs(
        tf
    )
    assert epic is not None
    assert epic.risk is Risk.MEDIUM
    assert epic.complexity is Complexity.MEDIUM
    assert floor.risk_floor is None
    assert signals is None
    assert complexity is None
    assert uncertain is False


def test_decision_inputs_without_epic_proposal(tmp_path: Path) -> None:
    path = _path(tmp_path)
    tf = create(path, 2, "standalone")
    epic, *_ = decision_inputs(tf)
    assert epic is None
