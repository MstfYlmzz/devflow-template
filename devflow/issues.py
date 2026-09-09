"""GitHub Issue loading for issue-backed task starts.

Uses the `gh` CLI (controller-side). Agents never call this.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    state: str


def fetch_issue(repo: Path, issue_id: int) -> GitHubIssue:
    """Load an issue via ``gh issue view``. Fail-closed on errors."""
    argv = [
        "gh",
        "issue",
        "view",
        str(issue_id),
        "--json",
        "number,title,body,state",
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GitHub CLI not found") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "gh issue view failed"
        lowered = detail.casefold()
        if "could not resolve to an issue" in lowered or "not found" in lowered:
            raise RuntimeError(f"issue {issue_id} not found")
        raise RuntimeError(detail)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh issue view returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("gh issue view returned invalid JSON")

    number = int(data.get("number", issue_id))
    title = str(data.get("title") or "").strip() or f"Issue {issue_id}"
    body = data.get("body")
    body_text = body.strip() if isinstance(body, str) and body.strip() else ""
    state = str(data.get("state") or "").strip().upper()
    if state == "CLOSED":
        raise RuntimeError(f"issue {issue_id} is closed")
    if state and state != "OPEN":
        raise RuntimeError(f"issue {issue_id} has unsupported state {state}")
    return GitHubIssue(number=number, title=title, body=body_text, state="OPEN")
