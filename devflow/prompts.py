"""Build agent prompts from role files and a closed task context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from devflow.taskfile import TaskFile

_ROLES = ("triage", "implementer", "reviewer")
_HEADING_RE = re.compile(r"^## ([^#].*?)\s*$", re.MULTILINE)
# Implementer artefacts. The reviewer may still explore the repo read-only
# in its own worktree (REVIEW mode): source, callers, and existing tests.
_REVIEWER_HIDDEN_HEADINGS = frozenset({"Plan", "Implementation"})


def build_prompt(
    role: str,
    task: TaskFile,
    repo: Path,
    extra: dict[str, Any],
    *,
    plan_only: bool = False,
) -> str:
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    role_path = repo / ".ai" / "roles" / f"{role}.md"
    if not role_path.is_file():
        raise FileNotFoundError(role_path)
    chunks = [
        role_path.read_text(encoding="utf-8").rstrip(),
        "",
        f"# Task {task.frontmatter.id}: {task.frontmatter.title}",
    ]
    if role == "reviewer":
        chunks.extend(_reviewer_context(task, repo, extra))
    elif role == "implementer":
        chunks.extend(_implementer_context(task, extra, plan_only=plan_only))
    else:
        chunks.extend(_triage_context(task, extra))
    text = "\n".join(chunks).rstrip() + "\n"
    return text


def _triage_context(task: TaskFile, extra: dict[str, Any]) -> list[str]:
    lines = ["", "## Issue", task.body.strip() or "(none)"]
    needed = extra.get("needed")
    if needed:
        lines.extend(["", f"Needed fields: {', '.join(needed)}"])
    return lines


def _implementer_context(
    task: TaskFile, extra: dict[str, Any], *, plan_only: bool
) -> list[str]:
    detail = extra.get("plan_detail", "none")
    lines = ["", f"plan_detail: {detail}"]
    if plan_only:
        lines.extend(["", "plan_only: true", "", _plan_only_instructions(str(detail))])
    lines.extend(["", task.body.strip() or "(empty task body)"])
    return lines


def _plan_only_instructions(detail: str) -> str:
    if detail == "formal":
        return (
            "Write only a plan. Do not change any code.\n"
            "Include a file list, architectural impact, and the test approach."
        )
    return "Write only a plan. Do not change any code.\nUse a short bullet list."


def _reviewer_context(task: TaskFile, repo: Path, extra: dict[str, Any]) -> list[str]:
    issue = _strip_reviewer_hidden(task.body).strip() or "(none)"
    previous = str(extra.get("previous_findings") or "(none)")
    return [
        "",
        "## Issue",
        issue,
        *_listed_docs(repo, "docs/requirements"),
        *_listed_docs(repo, "docs/adr"),
        "",
        "## Diff",
        str(extra.get("diff") or "(none)"),
        "",
        "## Verify",
        str(extra.get("verify_output") or "(none)"),
        "",
        "## Previous findings",
        previous,
    ]


def _listed_docs(repo: Path, rel: str) -> list[str]:
    folder = repo / rel
    lines = ["", f"## {rel}"]
    if not folder.is_dir():
        lines.append("(none)")
        return lines
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    if not files:
        lines.append("(none)")
        return lines
    for path in files:
        lines.append(f"### {path.relative_to(repo).as_posix()}")
        lines.append(path.read_text(encoding="utf-8"))
    return lines


def _strip_reviewer_hidden(body: str) -> str:
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return body
    kept = [body[: matches[0].start()]]
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if heading in _REVIEWER_HIDDEN_HEADINGS:
            continue
        kept.append(body[match.start() : end])
    return "".join(kept)
