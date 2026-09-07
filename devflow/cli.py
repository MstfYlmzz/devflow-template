from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from devflow.init import expected_relative_paths, init
from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    EpicProposal,
    Risk,
    RoutingDecision,
    apply_floor,
    check_merge_gate,
    decide,
    load_policy,
    needed_triage_fields,
    validate_policy,
)

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

    policy_path = root / ".ai" / "policy.yml"
    if policy_path.is_file():
        try:
            for message in validate_policy(load_policy(policy_path)):
                emit("error", message)
        except ValueError as exc:
            emit("error", str(exc))

    print(f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def _cmd_doctor() -> int:
    return doctor(Path.cwd())


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _policy_path() -> Path:
    cwd_policy = Path.cwd() / ".ai" / "policy.yml"
    if cwd_policy.is_file():
        return cwd_policy
    bundled = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "project"
        / ".ai"
        / "policy.yml"
    )
    if bundled.is_file():
        return bundled
    raise FileNotFoundError(".ai/policy.yml not found")


def _cmd_classify(
    *,
    paths_arg: str,
    labels_arg: str,
    risk_arg: str | None,
    epic_risk_arg: str | None,
    epic_complexity_arg: str | None,
) -> int:
    try:
        policy = load_policy(_policy_path())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    user_hint: Risk | None = None
    if risk_arg:
        try:
            user_hint = Risk(risk_arg.strip().upper())
        except ValueError:
            print(f"invalid risk: {risk_arg}", file=sys.stderr)
            return 2

    epic: EpicProposal | None = None
    if epic_risk_arg or epic_complexity_arg:
        try:
            epic_risk = Risk(epic_risk_arg.strip().upper()) if epic_risk_arg else None
            epic_complexity = (
                Complexity(epic_complexity_arg.strip().upper())
                if epic_complexity_arg
                else None
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        epic = EpicProposal(risk=epic_risk, complexity=epic_complexity, reason=None)

    paths = _split_csv(paths_arg)
    labels = _split_csv(labels_arg)
    floor = apply_floor(paths, labels, policy)
    needed = needed_triage_fields(floor, epic)
    decision = decide(
        floor=floor,
        signals=None,
        complexity=None,
        architecture_impact=ArchitectureImpact.NONE,
        uncertain=False,
        user_risk_hint=user_hint,
        paths=paths,
        policy=policy,
        epic=epic,
    )

    risk_reason = ""
    floor_glob = (
        floor.matched_rules[0].split(" (path:", 1)[0] if floor.matched_rules else ""
    )
    if epic is not None and epic.risk is not None:
        if floor.risk_floor is not None and floor.risk_floor is not epic.risk:
            risk_reason = f"  (epic proposal, raised by floor: {floor_glob})"
        else:
            risk_reason = "  (epic proposal)"
    elif floor.risk_floor is not None and floor_glob:
        risk_reason = f"  (floor: {floor_glob})"
    elif user_hint is not None:
        risk_reason = "  (user hint)"

    complexity_needed = "complexity" in needed
    if complexity_needed:
        complexity_text = "? (triage needed)"
        implementer_text = "? (depends on complexity)"
    elif epic is not None and epic.complexity is not None:
        complexity_text = f"{decision.complexity.value}  (epic proposal)"
        implementer_text = decision.implementer
    else:
        complexity_text = decision.complexity.value
        implementer_text = decision.implementer

    review_text = "required" if decision.review_required else "not required"
    bypass_text = f"allowed (friction: {decision.bypass_friction})"

    print(f"{'risk:':<13}{decision.risk.value}{risk_reason}")
    print(f"{'complexity:':<13}{complexity_text}")
    print(f"{'implementer:':<13}{implementer_text}")
    print(f"{'review:':<13}{review_text}")
    print(f"{'bypass:':<13}{bypass_text}")
    print()
    if needed:
        print(f"triage needed for: {', '.join(needed)}")
    else:
        print("triage: not needed (epic decision present)")
    return 0


def _stub_decision(risk: Risk) -> RoutingDecision:
    return RoutingDecision(
        risk=risk,
        complexity=Complexity.MEDIUM,
        architecture_impact=ArchitectureImpact.NONE,
        implementer="cursor",
        plan_required=False,
        plan_approval_required=False,
        review_required=False,
        evidence_required=False,
        bypass_allowed=True,
        bypass_friction="none" if risk is Risk.LOW else "reason",
        reasons=[],
    )


def _cmd_check_merge(*, risk_arg: str, paths_arg: str) -> int:
    try:
        policy = load_policy(_policy_path())
        previous = Risk(risk_arg.strip().upper())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    paths = _split_csv(paths_arg)
    result = check_merge_gate(_stub_decision(previous), paths, policy)
    glob = ""
    if result.matched_rules:
        glob = f"  ({result.matched_rules[0].split(' (path:', 1)[0]})"
    print(f"{'previous:':<13}{result.previous_risk.value}")
    print(f"{'actual:':<13}{result.actual_risk.value}{glob}")
    if result.passed:
        print(f"{'result:':<13}OK")
        return 0
    print(f"{'result:':<13}{result.reason}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="devflow")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--force", action="store_true")

    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--paths", required=True)
    classify_parser.add_argument("--labels", default="")
    classify_parser.add_argument("--risk", default=None)
    classify_parser.add_argument("--epic-risk", default=None)
    classify_parser.add_argument("--epic-complexity", default=None)

    merge_parser = sub.add_parser("check-merge")
    merge_parser.add_argument("--risk", required=True)
    merge_parser.add_argument("--paths", required=True)

    sub.add_parser("doctor")

    args = parser.parse_args()
    if args.command == "init":
        raise SystemExit(_cmd_init(force=args.force))
    if args.command == "classify":
        raise SystemExit(
            _cmd_classify(
                paths_arg=args.paths,
                labels_arg=args.labels,
                risk_arg=args.risk,
                epic_risk_arg=args.epic_risk,
                epic_complexity_arg=args.epic_complexity,
            )
        )
    if args.command == "check-merge":
        raise SystemExit(_cmd_check_merge(risk_arg=args.risk, paths_arg=args.paths))
    raise SystemExit(_cmd_doctor())
