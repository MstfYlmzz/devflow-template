from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from devflow.freshness import ReviewRecord
from devflow.policy import Complexity, Risk, TriageSignals, apply_floor, load_policy
from devflow.runtime import RuntimeChoice, RuntimeSelection
from devflow.taskfile import (
    TaskFrontmatter,
    append_section,
    atomic_write,
    body_sections,
    create,
    decision_inputs,
    estimate_paths,
    format_estimated_floor_matches,
    read,
    read_doc_impact,
    redact,
    update_frontmatter,
)

_REPO_POLICY = Path(__file__).resolve().parents[1] / ".ai" / "policy.yml"


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


def test_redact_skips_long_windows_path() -> None:
    path = (
        r"C:\Users\MustafaYilmaz\Desktop\Projects\devflow-template"
        r"\.devflow\worktrees\wt-184\scripts\verify"
    )
    assert redact(path) == path
    posix = (
        "C:/Users/MustafaYilmaz/Desktop/Projects/devflow-template"
        "/.devflow/worktrees/wt-184/scripts/verify"
    )
    assert redact(posix) == posix


def test_redact_long_base64_token() -> None:
    token = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
    assert len(token) >= 40
    text = redact(f"auth {token} leftover")
    assert token not in text
    assert "[REDACTED]" in text


def test_runtime_selection_round_trips_and_old_tasks_default_to_none(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "old.md"
    old_path.write_text(
        "---\nid: 1\ntitle: Old task\nstate: IMPLEMENTING\n---\n",
        encoding="utf-8",
    )
    assert read(old_path).frontmatter.runtime_selection is None

    path = tmp_path / "task.md"
    selection = RuntimeSelection(
        triage=RuntimeChoice(model="gpt-triage", effort="medium"),
        implementer=RuntimeChoice(model="gpt-code", effort="high"),
        reviewer=RuntimeChoice(model="opus", effort="high"),
    )
    create(path, 2, "Runtime task", runtime_selection=selection)
    assert read(path).frontmatter.runtime_selection == selection
    text = path.read_text(encoding="utf-8")
    assert "runtime_selection:" in text
    assert "provider:" not in text


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
    assert "review_records" in names
    assert "blocked_reason" in names


def test_review_records_roundtrip_does_not_redact_sha(tmp_path: Path) -> None:
    path = _path(tmp_path)
    sha = "0123456789abcdef0123456789abcdef01234567"
    record = ReviewRecord(
        head_sha=sha,
        base_sha=sha,
        round=2,
        blocking_findings=0,
        unverified_high=1,
        timestamp="2026-01-01T00:00:00Z",
    )
    create(path, 184, "t", review_records=[record])
    updated = update_frontmatter(path, review_records=[record])
    text = path.read_text(encoding="utf-8")
    assert sha in text
    assert updated.frontmatter.review_records[0].head_sha == sha
    assert updated.frontmatter.review_records[0].unverified_high == 1


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


def test_doc_impact_latest_valid_supersedes_invalid(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Doc impact", "Status: none\n")
    before = path.read_text(encoding="utf-8")
    assert "Status: none" in before
    tf = append_section(path, "Doc impact", "status: none\nfiles: []\n")
    after = path.read_text(encoding="utf-8")
    assert "Status: none" in after
    assert after.startswith(before.rstrip("\n")) or before in after
    impact = read_doc_impact(tf)
    assert impact is not None
    assert impact.status == "none"
    assert impact.files == []


def test_doc_impact_latest_invalid_does_not_fallback(tmp_path: Path) -> None:
    path = _path(tmp_path)
    create(path, 1, "t")
    append_section(path, "Doc impact", "status: none\nfiles: []\n")
    tf = append_section(path, "Doc impact", "Status: none\n")
    with pytest.raises(ValueError, match="invalid Doc impact"):
        read_doc_impact(tf)


def test_implementer_doc_impact_role_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    root_text = (root / ".ai" / "roles" / "implementer.md").read_text(encoding="utf-8")
    template_text = (
        root / "templates" / "project" / ".ai" / "roles" / "implementer.md"
    ).read_text(encoding="utf-8")
    assert root_text == template_text
    assert "status: follow-up" not in root_text
    assert "status: none" in root_text
    assert "status: updated" in root_text
    assert "status: adr_required" in root_text
    assert "lowercase" in root_text.casefold()
    assert "Status must be one of" not in root_text
    assert "do not use `follow-up`" in root_text.casefold()

    for index, section in enumerate(
        (
            "status: none\nfiles: []\n",
            "status: adr_required\nfiles: []\nadr: ADR-XXX\n",
        )
    ):
        path = _path(tmp_path, f"role-{index}.md")
        create(path, index + 10, "t")
        tf = append_section(path, "Doc impact", section)
        impact = read_doc_impact(tf)
        assert impact is not None
        assert impact.status in {"none", "adr_required"}


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


def test_atomic_write_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    atomic_write(path, "hello\n")
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_atomic_write_keeps_original_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("original\n", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        atomic_write(path, "new content\n")
    assert path.read_text(encoding="utf-8") == "original\n"


def test_estimate_paths_authority_matches_devflow_floor(tmp_path: Path) -> None:
    policy = load_policy(_REPO_POLICY)
    tf = create(
        _path(tmp_path),
        1,
        "coverage",
        modules=["authority"],
        body="no file paths here\n",
    )
    paths = estimate_paths(tf)
    assert paths == [
        "devflow/authority.py",
        "src/authority/**",
        "**/authority/**",
        "**/*authority*",
    ]
    floor = apply_floor(paths, [], policy)
    assert floor.risk_floor is Risk.HIGH
    assert any("devflow/**" in item for item in floor.matched_rules)
    recorded = format_estimated_floor_matches(floor.matched_rules, tf)
    assert recorded == ["devflow/** (from module: authority)"]


def test_estimate_paths_empty_when_no_modules_or_issue_paths(tmp_path: Path) -> None:
    policy = load_policy(_REPO_POLICY)
    tf = create(
        _path(tmp_path, "2.md"),
        2,
        "coverage",
        modules=[],
        body="see pyproject.toml — not a path with a slash\n",
    )
    paths = estimate_paths(tf)
    assert paths == []
    floor = apply_floor(paths, [], policy)
    assert floor.risk_floor is None
    assert floor.matched_rules == []
