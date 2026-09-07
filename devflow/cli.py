from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from devflow.init import expected_relative_paths, init

_TODO_MARK = "<!-- TODO:"
_EXPECTED_DIRS: tuple[str, ...] = (
    ".devflow/tasks",
    "docs/architecture",
    "docs/requirements",
    "docs/adr",
    ".ai",
)
_EXPECTED_AI_FILES: tuple[str, ...] = (
    ".ai/policy.yml",
    ".ai/roles/triage.md",
    ".ai/roles/implementer.md",
    ".ai/roles/reviewer.md",
    ".ai/review-schema.md",
)


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _cmd_init(*, force: bool) -> int:
    root = Path.cwd()
    try:
        report = init(root, force=force)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for path in report.created:
        print(_display(path, root))
    for path in report.skipped:
        print(f"skipped (exists): {_display(path, root)}")
    print("next: add language-specific stages to scripts/verify.d/")
    print("      then run ./scripts/setup-hooks")
    return 0


def _hooks_path(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _line(level: str, message: str) -> str:
    return f"{level + ':':<7}{message}"


def _language_stages(verify_d: Path) -> list[Path]:
    if not verify_d.is_dir():
        return []
    stages: list[Path] = []
    for path in verify_d.iterdir():
        if not path.is_file() or path.name in {".gitkeep", "00-preflight"}:
            continue
        stages.append(path)
    return stages


def _todo_counts(root: Path) -> tuple[int, int]:
    comments = 0
    files = 0
    for md in sorted(root.rglob("*.md")):
        if any(part in {".git", ".venv"} for part in md.parts):
            continue
        count = md.read_text(encoding="utf-8").count(_TODO_MARK)
        if count:
            comments += count
            files += 1
    return comments, files


def _expected_files() -> list[Path]:
    rels = expected_relative_paths()
    for name in _EXPECTED_AI_FILES:
        rel = Path(name)
        if rel not in rels:
            rels.append(rel)
    return rels


def doctor(root: Path) -> int:
    errors = 0
    warnings = 0

    def emit(level: str, message: str) -> None:
        nonlocal errors, warnings
        print(_line(level, message))
        if level == "error":
            errors += 1
        elif level == "warn":
            warnings += 1

    expected_files = _expected_files()
    missing_files = [path for path in expected_files if not (root / path).is_file()]
    present_files = len(expected_files) - len(missing_files)
    if missing_files:
        for path in missing_files:
            emit("error", f"missing file: {path.as_posix()}")
    else:
        emit("ok", f"files present ({present_files})")

    missing_dirs = [name for name in _EXPECTED_DIRS if not (root / name).is_dir()]
    if missing_dirs:
        for name in missing_dirs:
            emit("error", f"missing directory: {name}")
    else:
        emit("ok", f"directories present ({len(_EXPECTED_DIRS)})")

    verify = root / "scripts" / "verify"
    if verify.is_file():
        if os.access(verify, os.X_OK):
            emit("ok", "scripts/verify is executable")
        else:
            emit("error", "scripts/verify is not executable")

    language_stages = _language_stages(root / "scripts" / "verify.d")
    if language_stages:
        emit("ok", f"verify stages present ({len(language_stages)})")
    else:
        emit("error", "no verify stages in scripts/verify.d/")

    hooks = _hooks_path(root)
    if hooks is None:
        emit("warn", "core.hooksPath not set — run ./scripts/setup-hooks")
    else:
        emit("ok", f"core.hooksPath is {hooks}")

    todo_comments, todo_files = _todo_counts(root)
    if todo_comments:
        emit("warn", f"{todo_comments} TODO comments in {todo_files} files")
    else:
        emit("ok", "no TODO comments")

    print(f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def _cmd_doctor() -> int:
    return doctor(Path.cwd())


def main() -> None:
    parser = argparse.ArgumentParser(prog="devflow")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--force", action="store_true")

    sub.add_parser("doctor")

    args = parser.parse_args()
    if args.command == "init":
        raise SystemExit(_cmd_init(force=args.force))
    raise SystemExit(_cmd_doctor())
