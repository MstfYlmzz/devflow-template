from __future__ import annotations

from pathlib import Path

from devflow.freshness import (
    ReviewRecord,
    check_code_freshness,
    check_decision_validity,
)
from devflow.taskfile import append_section, create
from tests.conftest import git


def _sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return _sha(repo)


def _app_repo(git_repo: Path) -> Path:
    repo = git_repo
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    task = repo / ".devflow" / "tasks"
    task.mkdir(parents=True)
    (task / "184.md").write_text("task\n", encoding="utf-8")
    _commit(repo, "initial")
    return repo


def _record(head: str, base: str) -> ReviewRecord:
    return ReviewRecord(
        head_sha=head,
        base_sha=base,
        round=1,
        blocking_findings=0,
        unverified_high=0,
        timestamp="2026-01-01T00:00:00Z",
    )


def test_code_freshness_same_head_is_fresh(git_repo: Path) -> None:
    repo = _app_repo(git_repo)
    head = _sha(repo)
    result = check_code_freshness(repo, _record(head, head), head, head)
    assert result.fresh is True
    assert result.changed_since_review == []


def test_code_freshness_only_task_file_change_is_fresh(git_repo: Path) -> None:
    repo = _app_repo(git_repo)
    reviewed = _sha(repo)
    (repo / ".devflow" / "tasks" / "184.md").write_text("reviewed\n", encoding="utf-8")
    current = _commit(repo, "write review into task file")
    result = check_code_freshness(
        repo,
        _record(reviewed, reviewed),
        current,
        reviewed,
    )
    assert result.fresh is True
    assert result.changed_since_review == []


def test_code_freshness_source_change_is_stale(git_repo: Path) -> None:
    repo = _app_repo(git_repo)
    reviewed = _sha(repo)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    current = _commit(repo, "change source")
    result = check_code_freshness(
        repo,
        _record(reviewed, reviewed),
        current,
        reviewed,
    )
    assert result.fresh is False
    assert result.changed_since_review == ["src/app.py", "tests/test_app.py"]
    assert "2 files changed since review" in result.reason


def test_code_freshness_base_moved_head_same_is_stale(git_repo: Path) -> None:
    repo = _app_repo(git_repo)
    reviewed = _sha(repo)
    git(repo, "checkout", "-b", "task")
    git(repo, "checkout", "main")
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    moved_base = _commit(repo, "main moved")
    git(repo, "checkout", "task")
    head = _sha(repo)
    assert head == reviewed
    result = check_code_freshness(
        repo,
        _record(head, reviewed),
        head,
        moved_base,
    )
    assert result.fresh is False
    assert result.changed_since_review == []
    assert result.reason == f"base moved from {reviewed} to {moved_base}"


def test_decision_validity_waiver_without_reason(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="READY_TO_MERGE")
    append_section(path, "Doc impact", "status: none\nfiles: []\n")
    tf = append_section(path, "Waivers", "- id: R-003\n  reason: ''\n")
    result = check_decision_validity(tf)
    assert result.valid is False
    assert "waiver for R-003 has no reason" in result.issues


def test_decision_validity_missing_doc_impact(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    tf = create(path, 1, "t", state="READY_TO_MERGE")
    result = check_decision_validity(tf)
    assert result.valid is False
    assert "doc impact section is missing or invalid" in result.issues


def test_decision_validity_collects_all_issues(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="REVIEW")
    tf = append_section(path, "Waivers", "- id: R-003\n  reason: ''\n")
    result = check_decision_validity(tf)
    assert result.valid is False
    assert "waiver for R-003 has no reason" in result.issues
    assert "doc impact section is missing or invalid" in result.issues
    assert "review was entered but review_records is empty" in result.issues
    assert len(result.issues) == 3


def test_decision_validity_clean_file(tmp_path: Path) -> None:
    path = tmp_path / "1.md"
    create(path, 1, "t", state="READY_TO_MERGE")
    tf = append_section(path, "Doc impact", "status: none\nfiles: []\n")
    result = check_decision_validity(tf)
    assert result.valid is True
    assert result.issues == []
