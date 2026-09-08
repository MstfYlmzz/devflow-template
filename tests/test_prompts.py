from __future__ import annotations

from pathlib import Path

import pytest

from devflow.prompts import build_prompt
from devflow.taskfile import create, read


def _role(repo: Path, name: str, body: str = "# role\n") -> None:
    path = repo / ".ai" / "roles" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _task(repo: Path) -> Path:
    path = repo / "task.md"
    create(
        path,
        184,
        "Order cancel",
        body=(
            "issue text here\n\n"
            "## Plan\n\nSECRET_PLAN\n\n"
            "## Implementation\n\nSECRET_IMPL\n\n"
            "## Notes\n\nkeep me\n"
        ),
    )
    return path


def test_reviewer_prompt_omits_plan_and_implementation(tmp_path: Path) -> None:
    _role(tmp_path, "reviewer", "# Reviewer\n")
    tf = read(_task(tmp_path))
    text = build_prompt(
        "reviewer",
        tf,
        tmp_path,
        {"diff": "diff-here", "verify_output": "verify-here"},
    )
    assert "## Plan" not in text
    assert "## Implementation" not in text
    assert "SECRET_PLAN" not in text
    assert "SECRET_IMPL" not in text
    assert "issue text here" in text
    assert "keep me" in text
    assert "diff-here" in text
    assert "verify-here" in text


def test_implementer_prompt_includes_body_and_plan_detail(tmp_path: Path) -> None:
    _role(tmp_path, "implementer", "# Implementer\n")
    tf = read(_task(tmp_path))
    text = build_prompt("implementer", tf, tmp_path, {"plan_detail": "formal"})
    assert "plan_detail: formal" in text
    assert "SECRET_PLAN" in text
    assert "issue text here" in text
    assert "# Implementer" in text


def test_implementer_plan_only_asks_for_plan_not_code(tmp_path: Path) -> None:
    _role(tmp_path, "implementer", "# Implementer\n")
    tf = read(_task(tmp_path))
    brief = build_prompt(
        "implementer",
        tf,
        tmp_path,
        {"plan_detail": "brief"},
        plan_only=True,
    )
    assert "plan_only: true" in brief
    assert "Write only a plan" in brief
    assert "Do not change any code" in brief
    assert "short bullet list" in brief
    formal = build_prompt(
        "implementer",
        tf,
        tmp_path,
        {"plan_detail": "formal"},
        plan_only=True,
    )
    assert "file list" in formal
    assert "architectural impact" in formal
    assert "test approach" in formal


def test_missing_role_file_raises(tmp_path: Path) -> None:
    tf = read(_task(tmp_path))
    with pytest.raises(FileNotFoundError):
        build_prompt("triage", tf, tmp_path, {})
