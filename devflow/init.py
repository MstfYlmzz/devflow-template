from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from devflow.lock import LOCKS_GITIGNORE_LINE

_EXTRA_RELATIVE: tuple[str, ...] = (
    "scripts/verify",
    "scripts/verify.d/00-preflight",
    ".githooks/pre-commit",
    ".githooks/pre-push",
    "scripts/setup-hooks",
)


@dataclass
class InitReport:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def expected_relative_paths() -> list[Path]:
    template = _source_root() / "templates" / "project"
    rels: list[Path] = []
    for path in sorted(template.rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            rels.append(path.relative_to(template))
    for extra in _EXTRA_RELATIVE:
        rel = Path(extra)
        if rel not in rels:
            rels.append(rel)
    return rels


def _is_git_repo(target: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _copy_file(src: Path, dest: Path, report: InitReport) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        report.skipped.append(dest)
        return
    dest.write_bytes(src.read_bytes())
    dest.chmod(src.stat().st_mode)
    report.created.append(dest)


def init(target: Path, force: bool = False) -> InitReport:
    if not force and not _is_git_repo(target):
        raise RuntimeError("not a git repository")

    report = InitReport()
    source_root = _source_root()
    template = source_root / "templates" / "project"

    for path in sorted(template.rglob("*")):
        rel = path.relative_to(template)
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        if path.name == ".gitkeep":
            dest.parent.mkdir(parents=True, exist_ok=True)
            continue
        _copy_file(path, dest, report)

    for extra in _EXTRA_RELATIVE:
        src = source_root / extra
        dest = target / extra
        _copy_file(src, dest, report)

    _ensure_locks_gitignore(target, report)
    return report


_WORKTREES_GITIGNORE_LINE = ".devflow/worktrees/"


def _gitignore_has_line(text: str, line: str) -> bool:
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == line or stripped == line.rstrip("/"):
            return True
    return False


def _ensure_locks_gitignore(target: Path, report: InitReport) -> None:
    path = target / ".gitignore"
    required = (LOCKS_GITIGNORE_LINE, _WORKTREES_GITIGNORE_LINE)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        missing = [line for line in required if not _gitignore_has_line(text, line)]
        if not missing:
            return
        suffix = "" if not text or text.endswith("\n") else "\n"
        path.write_text(
            f"{text}{suffix}" + "".join(f"{line}\n" for line in missing),
            encoding="utf-8",
        )
        return
    path.write_text("".join(f"{line}\n" for line in required), encoding="utf-8")
    report.created.append(path)
