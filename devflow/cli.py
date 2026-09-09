from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from devflow.agents import (
    COMMANDS,
    AgentMode,
    _defined_modes,
    _resolve_command,
    run,
)
from devflow.authority import load_policy_from_base
from devflow.ci_checks import changed_files, ci_checks_report, needs_fast_lane
from devflow.freshness import (
    check_code_freshness,
    check_decision_validity,
    decision_validity_blockers,
    latest_review_record,
)
from devflow.gitops import (
    git_output,
    inspect_resume,
    remove_task_worktree,
    task_branch_name,
    task_worktree,
)
from devflow.init import expected_relative_paths, init
from devflow.lock import (
    LOCKS_GITIGNORE_LINE,
    ignores_lock_dir,
    is_process_alive,
    read_lock,
    release,
)
from devflow.paths import repo_root, task_file
from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    EpicProposal,
    MergeGateResult,
    Risk,
    RoutingDecision,
    apply_floor,
    check_merge_gate,
    decide,
    fast_lane_eligible,
    load_policy,
    needed_triage_fields,
    validate_policy,
)
from devflow.runner import RunnerError, StartResult, approve, cancel, start, stop
from devflow.states import (
    InvalidTransition,
    State,
    Trigger,
    check_ready_to_merge,
    review_cycle_count,
    transition,
)
from devflow.taskfile import (
    TaskFile,
    append_section,
    body_sections,
    decision_inputs,
    estimate_paths,
    read,
    read_doc_impact,
    update_frontmatter,
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

    gitignore = root / ".gitignore"
    if gitignore.is_file() and ignores_lock_dir(gitignore.read_text(encoding="utf-8")):
        emit("ok", f"{LOCKS_GITIGNORE_LINE} is gitignored")
    else:
        emit("warn", f".gitignore does not ignore {LOCKS_GITIGNORE_LINE}")

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
        plan_detail_text = "? (depends on complexity)"
    elif epic is not None and epic.complexity is not None:
        complexity_text = f"{decision.complexity.value}  (epic proposal)"
        implementer_text = decision.implementer
        plan_detail_text = decision.plan_detail
    else:
        complexity_text = decision.complexity.value
        implementer_text = decision.implementer
        plan_detail_text = decision.plan_detail

    review_text = "required" if decision.review_required else "not required"
    bypass_text = f"allowed (friction: {decision.bypass_friction})"

    print(f"{'risk:':<13}{decision.risk.value}{risk_reason}")
    print(f"{'complexity:':<13}{complexity_text}")
    print(f"{'implementer:':<13}{implementer_text}")
    print(f"{'plan_detail:':<13}{plan_detail_text}")
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
        plan_detail="brief",
        review_required=False,
        evidence_required=False,
        bypass_allowed=True,
        bypass_friction="none" if risk is Risk.LOW else "reason",
        reasons=[],
    )


def _cmd_check_merge(*, risk_arg: str, paths_arg: str) -> int:
    try:
        policy = load_policy_from_base(repo_root())
        previous = Risk(risk_arg.strip().upper())
    except (OSError, ValueError, RuntimeError) as exc:
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


def _cmd_agent_check() -> int:
    missing = False
    for name, env_name in COMMANDS.items():
        argv = _resolve_command(name)
        label = f"{name}:"
        if argv is None:
            print(f"{label:<9}not configured — set {env_name}")
            missing = True
            continue
        binary = argv[0]
        found = shutil.which(binary) is not None or Path(binary).is_file()
        shown = Path(binary).name
        modes = _defined_modes(name)
        if modes:
            modes_text = ", ".join(mode.value for mode in modes)
        else:
            modes_text = "none (flags not defined)"
        if found:
            print(f"{label:<9}configured ({shown}) — modes: {modes_text}")
        else:
            print(
                f"{label:<9}configured ({shown}), not found in PATH"
                f" — modes: {modes_text}"
            )
            missing = True
    return 1 if missing else 0


_SMOKE_SOURCE = "def add(a, b):\n    return a - b\n"
_SMOKE_PROMPT = "fix the bug"


def _cmd_agent_smoke(*, agent: str) -> int:
    with tempfile.TemporaryDirectory(prefix="devflow-smoke-") as tmp:
        worktree = Path(tmp)
        # Codex (and similar CLIs) refuse non-git directories unless
        # --skip-git-repo-check is set; keep production flags strict and make
        # the smoke tree a real repo instead.
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "smoke@devflow.local"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Devflow Smoke"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        target = worktree / "add.py"
        target.write_text(_SMOKE_SOURCE, encoding="utf-8")
        prompt_file = worktree / "prompt.txt"
        prompt_file.write_text(_SMOKE_PROMPT, encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "smoke base"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )

        run(agent, prompt_file, worktree, AgentMode.READ_ONLY)
        read_only_ok = (
            target.is_file() and target.read_text(encoding="utf-8") == _SMOKE_SOURCE
        )
        if read_only_ok:
            print("read_only: file unchanged — OK")
        else:
            print("read_only: file modified — FAIL")

        target.write_text(_SMOKE_SOURCE, encoding="utf-8")
        run(agent, prompt_file, worktree, AgentMode.EDIT)
        edit_ok = (
            target.is_file() and target.read_text(encoding="utf-8") != _SMOKE_SOURCE
        )
        if edit_ok:
            print("edit:      file modified — OK")
        else:
            print("edit:      file unchanged — FAIL")

        if read_only_ok and edit_ok:
            print(f"{agent}: permission model verified")
            return 0
        return 1


def _cmd_task_show(*, task_id: int) -> int:
    path = task_file(task_id)
    if not path.is_file():
        print(f"task file not found: {path}", file=sys.stderr)
        return 1
    try:
        policy = load_policy(_policy_path())
        tf = read(path)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    epic, floor, signals, complexity, architecture_impact, uncertain = decision_inputs(
        tf, policy
    )
    needed = needed_triage_fields(floor, epic)
    decision = decide(
        floor=floor,
        signals=signals,
        complexity=complexity,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
        user_risk_hint=None,
        paths=estimate_paths(tf),
        policy=policy,
        epic=epic,
    )
    fm = tf.frontmatter
    epic_name = fm.epic if fm.epic else "(none)"
    if epic is None:
        epic_proposed = "(none)"
    else:
        risk_text = epic.risk.value if epic.risk is not None else "(none)"
        complexity_text = (
            epic.complexity.value if epic.complexity is not None else "(none)"
        )
        epic_proposed = f"{risk_text} / {complexity_text}"
    floor_text = ", ".join(floor.matched_rules) if floor.matched_rules else "(none)"
    triage_text = "not needed" if not needed else f"needed ({', '.join(needed)})"
    review_text = "required" if decision.review_required else "not required"
    sections = body_sections(tf)
    sections_text = ", ".join(sections) if sections else "(none)"
    impact = read_doc_impact(tf)
    if impact is None:
        impact_text = "(none)"
    elif impact.status == "updated" and impact.files:
        impact_text = f"{impact.status} ({', '.join(impact.files)})"
    elif impact.status == "adr_required" and impact.adr:
        impact_text = f"{impact.status} ({impact.adr})"
    else:
        impact_text = impact.status

    print(f"task {fm.id} — {fm.title}")
    print(f"epic:  {epic_name}")
    print(f"state: {fm.state}")
    print()
    print("inputs:")
    print(f"  {'epic proposed:':<16}{epic_proposed}")
    print(f"  {'floor matched:':<16}{floor_text}")
    print(f"  {'triage:':<16}{triage_text}")
    print()
    print("computed:")
    print(f"  {'risk:':<13}{decision.risk.value}")
    print(f"  {'complexity:':<13}{decision.complexity.value}")
    print(f"  {'implementer:':<13}{decision.implementer}")
    print(f"  {'review:':<13}{review_text}")
    print()
    print(f"sections: {sections_text}")
    print(f"doc impact: {impact_text}")
    return 0


def _routing_decision(tf: TaskFile) -> RoutingDecision:
    policy = load_policy(_policy_path())
    epic, floor, signals, complexity, architecture_impact, uncertain = decision_inputs(
        tf, policy
    )
    return decide(
        floor=floor,
        signals=signals,
        complexity=complexity,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
        user_risk_hint=None,
        paths=estimate_paths(tf),
        policy=policy,
        epic=epic,
    )


def _parse_blocked_from(value: str | None) -> State | None:
    if value is None:
        return None
    try:
        return State(value)
    except ValueError:
        return None


def _cmd_task_transition(
    *,
    task_id: int,
    trigger_arg: str,
    dry_run: bool,
) -> int:
    path = task_file(task_id)
    if not path.is_file():
        print(f"task file not found: {path}", file=sys.stderr)
        return 1
    trigger = Trigger(trigger_arg)
    try:
        tf = read(path)
        current = State(tf.frontmatter.state)
        decision = _routing_decision(tf)
        result = transition(
            current,
            trigger,
            decision,
            has_epic_decision=(
                tf.frontmatter.risk_proposed is not None
                and tf.frontmatter.complexity_proposed is not None
            ),
            review_cycles=review_cycle_count(tf),
            blocked_from=_parse_blocked_from(tf.frontmatter.blocked_from),
        )
    except InvalidTransition as exc:
        print(f"{task_id}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not dry_run:
        updates: dict[str, object] = {"state": result.to_state.value}
        if result.to_state is State.BLOCKED:
            updates["blocked_from"] = result.from_state.value
        elif result.from_state is State.BLOCKED:
            updates["blocked_from"] = None
        update_frontmatter(path, **updates)
        append_section(
            path,
            "Transition",
            (
                f"{result.from_state.value} → {result.to_state.value}"
                f" ({result.trigger.value})"
            ),
        )

    print(
        f"{task_id}: {result.from_state.value} → {result.to_state.value}"
        f"  ({result.trigger.value})"
    )
    return 0


def _short_sha(sha: str) -> str:
    return sha[:7] if len(sha) >= 7 else sha


def _resolve_head_and_base(root: Path) -> tuple[str, str]:
    head = git_output(root, "rev-parse", "HEAD")
    last_error = "cannot resolve base ref"
    for ref in ("origin/main", "main", "master"):
        try:
            return head, git_output(root, "rev-parse", ref)
        except RuntimeError as exc:
            last_error = str(exc)
    raise RuntimeError(last_error)


def _passed_merge_gate() -> MergeGateResult:
    return MergeGateResult(
        passed=True,
        previous_risk=Risk.LOW,
        actual_risk=Risk.LOW,
        matched_rules=[],
        reason=None,
    )


def _cmd_task_freshness(*, task_id: int) -> int:
    path = task_file(task_id)
    if not path.is_file():
        print(f"task file not found: {path}", file=sys.stderr)
        return 1
    try:
        tf = read(path)
        root = repo_root()
        current_head, current_base = _resolve_head_and_base(root)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    record = latest_review_record(tf)
    freshness = None
    if record is not None:
        try:
            freshness = check_code_freshness(root, record, current_head, current_base)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    validity = check_decision_validity(tf)
    latest = record
    unverified = 0 if latest is None else latest.unverified_high
    blocking_findings = 0 if latest is None else latest.blocking_findings
    readiness = check_ready_to_merge(
        tf,
        _stub_decision(Risk.LOW),
        True,
        blocking_findings,
        True,
        _passed_merge_gate(),
        freshness,
    )

    print(f"task {tf.frontmatter.id}")
    if record is None:
        print("review round: (none)")
    else:
        review_label = f"review round {record.round}:"
        current_label = "current:"
        width = max(len(review_label), len(current_label))
        print(
            f"{review_label:<{width}} head {_short_sha(record.head_sha)}, "
            f"base {_short_sha(record.base_sha)}"
        )
        print(
            f"{current_label:<{width}} head {_short_sha(current_head)}, "
            f"base {_short_sha(current_base)}"
        )
        print()
    if record is None:
        print(
            f"current:        head {_short_sha(current_head)}, "
            f"base {_short_sha(current_base)}"
        )
        print()

    if freshness is None:
        freshness_text = "(no review record)"
    elif freshness.fresh:
        freshness_text = "fresh"
    else:
        freshness_text = f"STALE — {freshness.reason}"
    print(f"{'code freshness:':<20}{freshness_text}")
    if freshness is not None and not freshness.fresh:
        for changed in freshness.changed_since_review:
            print(f"  {changed}")

    validity_issues = decision_validity_blockers(validity)
    if validity_issues:
        print(f"{'decision validity:':<20}INVALID")
        for issue in validity_issues:
            print(f"  {issue}")
    else:
        print(f"{'decision validity:':<20}valid")

    if unverified:
        print(f"{'unverified HIGH:':<20}{unverified} — needs human decision")
    else:
        print(f"{'unverified HIGH:':<20}0")

    print()
    if readiness.ready:
        print("merge gate: open")
        return 0
    print(f"merge gate: blocked ({len(readiness.blockers)})")
    return 1


def _lock_clock(started_at: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(started_at).strftime("%H:%M")
    except ValueError:
        return started_at


def _cmd_recover(*, task_id: int, release_lock: bool, abandon: bool) -> int:
    try:
        root = repo_root()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    path = task_file(task_id)
    if not path.is_file():
        print(f"task file not found: {path}", file=sys.stderr)
        return 1
    try:
        tf = read(path)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    lock = read_lock(task_id, root)
    resume = inspect_resume(task_id, root)
    worktree = task_worktree(root, task_id)
    rel_worktree = worktree.resolve().relative_to(root.resolve()).as_posix()
    if resume.worktree_exists:
        n = len(resume.uncommitted_files)
        worktree_text = f"{rel_worktree} (exists, {n} uncommitted files)"
    else:
        worktree_text = f"{rel_worktree} (missing)"
    branch = task_branch_name(root, task_id)
    branch_text = f"{branch} (exists)" if branch else "(none)"

    if lock is None:
        header = f"task {task_id} — no lock"
        lock_lines: list[str] = []
    elif is_process_alive(lock.pid):
        header = f"task {task_id} — lock held"
        lock_lines = [
            f"  pid {lock.pid} (running), stage: {lock.stage}, "
            f"started {_lock_clock(lock.started_at)}"
        ]
    else:
        header = f"task {task_id} — stale lock detected"
        lock_lines = [
            f"  pid {lock.pid} (not running), stage: {lock.stage}, "
            f"started {_lock_clock(lock.started_at)}"
        ]

    if not release_lock and not abandon:
        print(header)
        for line in lock_lines:
            print(line)
        print()
        print(f"state:     {tf.frontmatter.state}")
        print(f"worktree:  {worktree_text}")
        print(f"branch:    {branch_text}")
        print("last agent run: no result recorded")
        print()
        print("the work is preserved. options:")
        print(
            f"  devflow recover {task_id} --release   "
            "release lock, keep work, set BLOCKED"
        )
        print(
            f"  devflow recover {task_id} --abandon   "
            "release lock, remove worktree, set CANCELLED"
        )
        return 0

    if lock is not None and is_process_alive(lock.pid):
        print(
            f"task {task_id} is already running (pid {lock.pid})",
            file=sys.stderr,
        )
        return 1

    if release_lock:
        release(task_id, root)
        from_state = tf.frontmatter.state
        update_frontmatter(
            path,
            state=State.BLOCKED.value,
            blocked_from=from_state,
            blocked_reason="INTERRUPTED",
        )
        append_section(
            path,
            "Recovery",
            "INTERRUPTED — stale lock released, work preserved",
        )
        print(f"task {task_id}: lock released, state BLOCKED, worktree kept")
        return 0

    release(task_id, root)
    remove_task_worktree(root, task_id)
    update_frontmatter(path, state=State.CANCELLED.value)
    print(f"task {task_id}: lock released, worktree removed, state CANCELLED")
    return 0


def _cmd_fast_lane_check(*, paths_arg: str) -> int:
    try:
        policy = load_policy(_policy_path())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    eligible, reason = fast_lane_eligible(_split_csv(paths_arg), policy)
    if eligible:
        print(f"eligible: yes — {reason}")
        return 0
    print(f"eligible: no — {reason}")
    return 1


def _cmd_ci_checks(*, base: str) -> int:
    try:
        root = repo_root()
        changed = changed_files(root, base)
        policy = {}
        if needs_fast_lane(changed):
            policy = load_policy(_policy_path())
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    code, text = ci_checks_report(changed, policy)
    print(text, end="")
    return code


def _cmd_start(
    *,
    task_id: int,
    risk_arg: str | None,
    skip_review: bool,
    review: str | None,
    reason: str | None,
    dry_run: bool,
) -> int:
    hint: Risk | None = None
    if risk_arg:
        try:
            hint = Risk(risk_arg.strip().upper())
        except ValueError:
            print(f"invalid risk: {risk_arg}", file=sys.stderr)
            return 2
    try:
        result = start(
            repo_root(),
            task_id,
            risk_hint=hint,
            skip_review=skip_review,
            review_advisory=review == "advisory",
            reason=reason,
            dry_run=dry_run,
        )
    except (RunnerError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return _start_exit(result)


def _cmd_approve(
    *,
    task_id: int,
    risk_arg: str | None,
    skip_review: bool,
    review: str | None,
    reason: str | None,
) -> int:
    hint: Risk | None = None
    if risk_arg:
        try:
            hint = Risk(risk_arg.strip().upper())
        except ValueError:
            print(f"invalid risk: {risk_arg}", file=sys.stderr)
            return 2
    try:
        result = approve(
            repo_root(),
            task_id,
            risk_hint=hint,
            skip_review=skip_review,
            review_advisory=review == "advisory",
            reason=reason,
        )
    except (RunnerError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return _start_exit(result)


def _cmd_stop(*, task_id: int) -> int:
    try:
        stop(repo_root(), task_id)
    except (RunnerError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_cancel(*, task_id: int, reason: str, discard: bool) -> int:
    try:
        cancel(repo_root(), task_id, reason=reason, discard=discard)
    except (RunnerError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _start_exit(result: StartResult) -> int:
    joined = "\n".join(result.messages)
    if "already running" in joined or "stale lock" in joined:
        return 1
    if result.final_state is State.BLOCKED:
        return 1
    if result.verify_passed is False:
        return 1
    return 0


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

    fast_parser = sub.add_parser("fast-lane-check")
    fast_parser.add_argument("--paths", required=True)

    ci_parser = sub.add_parser("ci-checks")
    ci_parser.add_argument("--base", default="origin/main")

    sub.add_parser("doctor")
    sub.add_parser("agent-check")

    smoke_parser = sub.add_parser("agent-smoke")
    smoke_parser.add_argument("--agent", required=True, choices=list(COMMANDS))

    task_parser = sub.add_parser("task")
    task_sub = task_parser.add_subparsers(dest="task_command", required=True)
    show_parser = task_sub.add_parser("show")
    show_parser.add_argument("task_id", type=int, metavar="id")
    trans_parser = task_sub.add_parser("transition")
    trans_parser.add_argument("task_id", type=int, metavar="id")
    trans_parser.add_argument(
        "--trigger",
        required=True,
        choices=[item.value for item in Trigger],
    )
    trans_parser.add_argument("--dry-run", action="store_true")
    freshness_parser = task_sub.add_parser("freshness")
    freshness_parser.add_argument("task_id", type=int, metavar="id")

    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("task_id", type=int, metavar="id")
    recover_flags = recover_parser.add_mutually_exclusive_group()
    recover_flags.add_argument("--release", action="store_true")
    recover_flags.add_argument("--abandon", action="store_true")

    start_parser = sub.add_parser("start")
    start_parser.add_argument("task_id", type=int, metavar="id")
    start_parser.add_argument("--risk", default=None)
    start_parser.add_argument("--skip-review", action="store_true")
    start_parser.add_argument("--review", choices=["advisory"], default=None)
    start_parser.add_argument("--reason", default=None)
    start_parser.add_argument("--dry-run", action="store_true")

    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("task_id", type=int, metavar="id")
    approve_parser.add_argument("--risk", default=None)
    approve_parser.add_argument("--skip-review", action="store_true")
    approve_parser.add_argument("--review", choices=["advisory"], default=None)
    approve_parser.add_argument("--reason", default=None)

    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("task_id", type=int, metavar="id")

    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("task_id", type=int, metavar="id")
    cancel_parser.add_argument("--reason", required=True)
    cancel_parser.add_argument("--discard", action="store_true")

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
    if args.command == "fast-lane-check":
        raise SystemExit(_cmd_fast_lane_check(paths_arg=args.paths))
    if args.command == "ci-checks":
        raise SystemExit(_cmd_ci_checks(base=args.base))
    if args.command == "agent-check":
        raise SystemExit(_cmd_agent_check())
    if args.command == "agent-smoke":
        raise SystemExit(_cmd_agent_smoke(agent=args.agent))
    if args.command == "recover":
        raise SystemExit(
            _cmd_recover(
                task_id=args.task_id,
                release_lock=args.release,
                abandon=args.abandon,
            )
        )
    if args.command == "start":
        raise SystemExit(
            _cmd_start(
                task_id=args.task_id,
                risk_arg=args.risk,
                skip_review=args.skip_review,
                review=args.review,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        )
    if args.command == "approve":
        raise SystemExit(
            _cmd_approve(
                task_id=args.task_id,
                risk_arg=args.risk,
                skip_review=args.skip_review,
                review=args.review,
                reason=args.reason,
            )
        )
    if args.command == "stop":
        raise SystemExit(_cmd_stop(task_id=args.task_id))
    if args.command == "cancel":
        raise SystemExit(
            _cmd_cancel(
                task_id=args.task_id,
                reason=args.reason,
                discard=args.discard,
            )
        )
    if args.command == "task":
        if args.task_command == "show":
            raise SystemExit(_cmd_task_show(task_id=args.task_id))
        if args.task_command == "freshness":
            raise SystemExit(_cmd_task_freshness(task_id=args.task_id))
        raise SystemExit(
            _cmd_task_transition(
                task_id=args.task_id,
                trigger_arg=args.trigger,
                dry_run=args.dry_run,
            )
        )
    raise SystemExit(_cmd_doctor())
