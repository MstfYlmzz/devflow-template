"""Read and write `.devflow/tasks/<id>.md` context files.

Frontmatter is mutable. The markdown body is append-only: existing
lines are never edited or deleted. Outcomes such as risk and
implementer are not stored; they are recomputed by policy.decide()
on each read.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

from devflow.freshness import ReviewRecord
from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    EpicProposal,
    PolicyResult,
    Risk,
    TriageSignals,
    apply_floor,
)
from devflow.runtime import RUNTIME_ROLES, RuntimeChoice, RuntimeSelection

DocImpactStatus = Literal["none", "updated", "adr_required"]
EnumT = TypeVar("EnumT", bound=enum.Enum)

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-ant-[A-Za-z0-9_-]+", "[REDACTED]"),
    (r"sk-[A-Za-z0-9_-]+", "[REDACTED]"),
    (r"github_pat_[A-Za-z0-9_-]+", "[REDACTED]"),
    (r"ghp_[A-Za-z0-9_-]+", "[REDACTED]"),
    (r"gho_[A-Za-z0-9_-]+", "[REDACTED]"),
    (r"AKIA[A-Z0-9]{16}", "[REDACTED]"),
    (r"(?i)\b(PASSWORD|TOKEN|SECRET|API_KEY)=(\S+)", r"\1=[REDACTED]"),
    (r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]"),
    # Skip matches that contain path separators; see _redact_long_token.
    (r"[A-Za-z0-9+/_=-]{40,}", "[REDACTED]"),
)

_HEADING_RE = re.compile(r"^## ([^#].*?)\s*$", re.MULTILINE)
_ISSUE_TOKEN = re.compile(r"[^\s`\"'<>()\[\]{}]+")
_COMPILED_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), replacement) for pattern, replacement in SECRET_PATTERNS
)


@dataclass
class TaskFrontmatter:
    id: int
    title: str
    epic: str | None
    state: str
    risk_proposed: Risk | None
    risk_reason: str | None
    complexity_proposed: Complexity | None
    modules: list[str]
    blocked_by: list[int]
    adr: list[str]
    floor_risk: Risk | None
    floor_matched: list[str]
    signals: TriageSignals | None
    architecture_impact: ArchitectureImpact | None
    uncertain: bool
    floor_risk_actual: Risk | None
    floor_matched_actual: list[str]
    blocked_from: str | None
    blocked_reason: str | None
    review_records: list[ReviewRecord]
    runtime_selection: RuntimeSelection | None


@dataclass
class TaskFile:
    frontmatter: TaskFrontmatter
    body: str
    path: Path


@dataclass
class DocImpact:
    status: DocImpactStatus
    files: list[str]
    adr: str | None


def _redact_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if any(sep in token for sep in ("/", "\\", ":")):
        return token
    return "[REDACTED]"


def redact(text: str) -> str:
    result = text
    for pattern, replacement in _COMPILED_SECRETS:
        if pattern.pattern == r"[A-Za-z0-9+/_=-]{40,}":
            result = pattern.sub(_redact_long_token, result)
        else:
            result = pattern.sub(replacement, result)
    return result


def body_sections(tf: TaskFile) -> list[str]:
    return [match.group(1).strip() for match in _HEADING_RE.finditer(tf.body)]


def estimate_paths(tf: TaskFile) -> list[str]:
    """Guess likely paths for the first floor pass.

    This is an estimate. The real diff is checked later at the merge
    gate. These candidates only feed apply_floor so a module name like
    ``authority`` can match ``devflow/**`` before any code exists.
    """
    return [path for path, _origin in _estimated(tf)]


def format_estimated_floor_matches(matched_rules: list[str], tf: TaskFile) -> list[str]:
    """Rewrite floor hits so estimates are not stored as real diff paths."""
    origins = {path: origin for path, origin in _estimated(tf)}
    formatted: list[str] = []
    seen: set[str] = set()
    for rule in matched_rules:
        glob, sep, rest = rule.partition(" (path: ")
        if sep:
            path = rest[:-1] if rest.endswith(")") else rest
            origin = origins.get(path)
            if origin:
                rule = f"{glob} (from {origin})"
        if rule not in seen:
            seen.add(rule)
            formatted.append(rule)
    return formatted


def _estimated(tf: TaskFile) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(item: str, origin: str) -> None:
        if item and item not in seen:
            seen.add(item)
            items.append((item, origin))

    for module in tf.frontmatter.modules:
        name = module.strip().replace("\\", "/").strip("/")
        if not name:
            continue
        origin = f"module: {name}"
        add(f"devflow/{name}.py", origin)
        add(f"src/{name}/**", origin)
        add(f"**/{name}/**", origin)
        add(f"**/*{name}*", origin)

    text = tf.body.replace("\\", "/")
    for raw in _ISSUE_TOKEN.findall(text):
        token = raw.strip(".,;:")
        if "/" not in token:
            continue
        suffix = Path(token).suffix
        if len(suffix) < 2 or not suffix[1:].isalnum():
            continue
        add(token, "issue")
    return items


def decision_inputs(
    tf: TaskFile,
    policy: dict[str, Any] | None = None,
) -> tuple[
    EpicProposal | None,
    PolicyResult,
    TriageSignals | None,
    Complexity | None,
    ArchitectureImpact,
    bool,
]:
    fm = tf.frontmatter
    epic: EpicProposal | None = None
    if (
        fm.risk_proposed is not None
        or fm.complexity_proposed is not None
        or fm.risk_reason is not None
    ):
        epic = EpicProposal(
            risk=fm.risk_proposed,
            complexity=fm.complexity_proposed,
            reason=fm.risk_reason,
        )
    if policy is None:
        floor = PolicyResult(
            risk_floor=fm.floor_risk,
            complexity_hint=None,
            architecture_block=False,
            matched_rules=list(fm.floor_matched),
        )
    else:
        floor = apply_floor(estimate_paths(tf), [], policy)
    architecture_impact = fm.architecture_impact or ArchitectureImpact.NONE
    return (
        epic,
        floor,
        fm.signals,
        None,
        architecture_impact,
        fm.uncertain,
    )


def read(path: Path) -> TaskFile:
    text = path.read_text(encoding="utf-8")
    yaml_text, body = _split_frontmatter(text)
    raw = yaml.safe_load(yaml_text)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("frontmatter must be a mapping")
    data: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"unknown frontmatter key: {key!r}")
        data[key] = value
    return TaskFile(frontmatter=_parse_frontmatter(data), body=body, path=path)


def create(
    path: Path,
    task_id: int,
    title: str,
    epic: str | None = None,
    *,
    state: str = "BACKLOG",
    risk_proposed: Risk | None = None,
    risk_reason: str | None = None,
    complexity_proposed: Complexity | None = None,
    modules: list[str] | None = None,
    blocked_by: list[int] | None = None,
    adr: list[str] | None = None,
    floor_risk: Risk | None = None,
    floor_matched: list[str] | None = None,
    signals: TriageSignals | None = None,
    architecture_impact: ArchitectureImpact | None = None,
    uncertain: bool = False,
    floor_risk_actual: Risk | None = None,
    floor_matched_actual: list[str] | None = None,
    blocked_from: str | None = None,
    blocked_reason: str | None = None,
    review_records: list[ReviewRecord] | None = None,
    runtime_selection: RuntimeSelection | None = None,
    body: str = "",
) -> TaskFile:
    if path.exists():
        raise FileExistsError(path)
    fm = TaskFrontmatter(
        id=task_id,
        title=redact(title),
        epic=redact(epic) if epic is not None else None,
        state=redact(state),
        risk_proposed=risk_proposed,
        risk_reason=redact(risk_reason) if risk_reason is not None else None,
        complexity_proposed=complexity_proposed,
        modules=_redact_str_list(modules or []),
        blocked_by=list(blocked_by or []),
        adr=_redact_str_list(adr or []),
        floor_risk=floor_risk,
        floor_matched=_redact_str_list(floor_matched or []),
        signals=signals,
        architecture_impact=architecture_impact,
        uncertain=uncertain,
        floor_risk_actual=floor_risk_actual,
        floor_matched_actual=_redact_str_list(floor_matched_actual or []),
        blocked_from=redact(blocked_from) if blocked_from is not None else None,
        blocked_reason=redact(blocked_reason) if blocked_reason is not None else None,
        review_records=list(review_records or []),
        runtime_selection=runtime_selection,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, fm, redact(body) if body else "")
    return read(path)


def update_frontmatter(path: Path, **fields: object) -> TaskFile:
    tf = read(path)
    known = {item.name for item in dataclasses.fields(TaskFrontmatter)}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise ValueError(f"unknown frontmatter field: {unknown[0]}")
    updates = {key: _redact_value(value) for key, value in fields.items()}
    fm = dataclasses.replace(tf.frontmatter, **updates)  # type: ignore[arg-type]
    _write(path, fm, tf.body)
    return read(path)


def append_section(path: Path, heading: str, content: str) -> TaskFile:
    tf = read(path)
    heading = redact(heading)
    content = redact(content)
    suffix = ""
    if tf.body and not tf.body.endswith("\n"):
        suffix += "\n"
    suffix += f"## {heading}\n"
    if content:
        suffix += "\n"
        suffix += content
        if not content.endswith("\n"):
            suffix += "\n"
    _write(path, tf.frontmatter, tf.body + suffix)
    return read(path)


def read_doc_impact(tf: TaskFile) -> DocImpact | None:
    section = _latest_section_content(tf.body, "Doc impact")
    if section is None:
        return None
    raw = yaml.safe_load(section) if section.strip() else None
    if not isinstance(raw, dict):
        raise ValueError("invalid Doc impact section")
    status = raw.get("status")
    if status not in ("none", "updated", "adr_required"):
        raise ValueError(f"invalid Doc impact status: {status!r}")
    files_raw = raw.get("files") or []
    if not isinstance(files_raw, list) or not all(
        isinstance(item, str) for item in files_raw
    ):
        raise ValueError("Doc impact files must be a list of strings")
    adr_raw = raw.get("adr")
    adr = str(adr_raw) if adr_raw is not None else None
    return DocImpact(status=status, files=list(files_raw), adr=adr)


def _frontmatter_names() -> frozenset[str]:
    return frozenset(item.name for item in dataclasses.fields(TaskFrontmatter))


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        raise ValueError("missing YAML frontmatter")
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        raise ValueError("missing YAML frontmatter")
    closing = re.search(r"(?m)^---\s*$", rest)
    if closing is None:
        raise ValueError("missing YAML frontmatter")
    yaml_text = rest[: closing.start()]
    body = rest[closing.end() :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return yaml_text, body


def _parse_frontmatter(data: dict[str, object]) -> TaskFrontmatter:
    unknown = sorted(set(data) - _frontmatter_names())
    if unknown:
        raise ValueError(f"unknown frontmatter key: {unknown[0]}")
    missing = [name for name in ("id", "title", "state") if name not in data]
    if missing:
        raise ValueError(f"frontmatter missing keys: {', '.join(missing)}")
    signals_raw = data.get("signals")
    return TaskFrontmatter(
        id=_parse_int(data["id"], "id"),
        title=_parse_str(data["title"], "title"),
        epic=_opt_str(data.get("epic")),
        state=_parse_str(data["state"], "state"),
        risk_proposed=_opt_enum(Risk, data.get("risk_proposed")),
        risk_reason=_opt_str(data.get("risk_reason")),
        complexity_proposed=_opt_enum(Complexity, data.get("complexity_proposed")),
        modules=_parse_str_list(data.get("modules"), "modules"),
        blocked_by=_parse_int_list(data.get("blocked_by"), "blocked_by"),
        adr=_parse_str_list(data.get("adr"), "adr"),
        floor_risk=_opt_enum(Risk, data.get("floor_risk")),
        floor_matched=_parse_str_list(data.get("floor_matched"), "floor_matched"),
        signals=_parse_signals(signals_raw),
        architecture_impact=_opt_enum(
            ArchitectureImpact, data.get("architecture_impact")
        ),
        uncertain=_parse_bool(data.get("uncertain", False), "uncertain"),
        floor_risk_actual=_opt_enum(Risk, data.get("floor_risk_actual")),
        floor_matched_actual=_parse_str_list(
            data.get("floor_matched_actual"), "floor_matched_actual"
        ),
        blocked_from=_opt_str(data.get("blocked_from")),
        blocked_reason=_opt_str(data.get("blocked_reason")),
        review_records=_parse_review_records(data.get("review_records")),
        runtime_selection=_parse_runtime_selection(data.get("runtime_selection")),
    )


def _parse_signals(raw: object) -> TriageSignals | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("signals must be a mapping")
    known = {item.name for item in dataclasses.fields(TriageSignals)}
    unknown = sorted(str(key) for key in raw if str(key) not in known)
    if unknown:
        raise ValueError(f"unknown frontmatter key: signals.{unknown[0]}")
    return TriageSignals(
        transaction_change=_parse_bool(
            raw.get("transaction_change", False), "signals.transaction_change"
        ),
        concurrency_sensitive=_parse_bool(
            raw.get("concurrency_sensitive", False), "signals.concurrency_sensitive"
        ),
        architecture_boundary_change=_parse_bool(
            raw.get("architecture_boundary_change", False),
            "signals.architecture_boundary_change",
        ),
        unfamiliar_area=_parse_bool(
            raw.get("unfamiliar_area", False), "signals.unfamiliar_area"
        ),
    )


def _opt_enum(enum_cls: type[EnumT], value: object) -> EnumT | None:
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    try:
        return enum_cls(text)
    except ValueError as exc:
        raise ValueError(f"invalid {enum_cls.__name__}: {value!r}") from exc


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _parse_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _parse_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _parse_str_list(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return list(value)


def _parse_int_list(value: object, name: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of integers")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{name} must be a list of integers")
        result.append(item)
    return result


def _parse_review_records(raw: object) -> list[ReviewRecord]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("review_records must be a list")
    records: list[ReviewRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each review record must be a mapping")
        records.append(
            ReviewRecord(
                head_sha=_parse_str(item.get("head_sha"), "review_records.head_sha"),
                base_sha=_parse_str(item.get("base_sha"), "review_records.base_sha"),
                round=_parse_int(item.get("round"), "review_records.round"),
                blocking_findings=_parse_int(
                    item.get("blocking_findings"),
                    "review_records.blocking_findings",
                ),
                unverified_high=_parse_int(
                    item.get("unverified_high"),
                    "review_records.unverified_high",
                ),
                timestamp=_parse_str(item.get("timestamp"), "review_records.timestamp"),
            )
        )
    return records


def _parse_runtime_selection(raw: object) -> RuntimeSelection | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("runtime_selection must be a mapping")
    unknown = sorted(str(key) for key in raw if str(key) not in RUNTIME_ROLES)
    if unknown:
        raise ValueError(f"unknown frontmatter key: runtime_selection.{unknown[0]}")

    def choice(role: str) -> RuntimeChoice:
        value = raw.get(role)
        if value is None:
            return RuntimeChoice()
        if not isinstance(value, dict):
            raise ValueError(f"runtime_selection.{role} must be a mapping")
        extra = sorted(str(key) for key in value if str(key) not in {"model", "effort"})
        if extra:
            raise ValueError(
                f"unknown frontmatter key: runtime_selection.{role}.{extra[0]}"
            )
        model = _opt_str(value.get("model"))
        effort = _opt_str(value.get("effort"))
        return RuntimeChoice(model=model, effort=effort)

    return RuntimeSelection(
        triage=choice("triage"),
        implementer=choice("implementer"),
        reviewer=choice("reviewer"),
    )


def _yaml_value(value: object) -> object:
    if isinstance(value, (Risk, Complexity, ArchitectureImpact)):
        return value.value
    if isinstance(value, ReviewRecord):
        return dataclasses.asdict(value)
    if isinstance(value, TriageSignals):
        return dataclasses.asdict(value)
    if isinstance(value, (RuntimeChoice, RuntimeSelection)):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_yaml_value(item) for item in value]
    return value


def _frontmatter_dict(fm: TaskFrontmatter) -> dict[str, object]:
    data: dict[str, object] = {}
    for item in dataclasses.fields(fm):
        data[item.name] = _yaml_value(getattr(fm, item.name))
    return data


def atomic_write(path: Path, content: str) -> None:
    """Write content via a temp file, fsync, then replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write(path: Path, fm: TaskFrontmatter, body: str) -> None:
    dumped = yaml.safe_dump(
        _frontmatter_dict(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    atomic_write(path, f"---\n{dumped}---\n{body}")


def _redact_str_list(values: list[str]) -> list[str]:
    return [redact(item) for item in values]


def _redact_value(value: object) -> object:
    if isinstance(value, ReviewRecord):
        return value
    if isinstance(value, RuntimeChoice):
        return RuntimeChoice(
            model=redact(value.model) if value.model is not None else None,
            effort=redact(value.effort) if value.effort is not None else None,
        )
    if isinstance(value, RuntimeSelection):
        return RuntimeSelection(
            triage=_redact_runtime_choice(value.triage),
            implementer=_redact_runtime_choice(value.implementer),
            reviewer=_redact_runtime_choice(value.reviewer),
        )
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_runtime_choice(value: RuntimeChoice) -> RuntimeChoice:
    return RuntimeChoice(
        model=redact(value.model) if value.model is not None else None,
        effort=redact(value.effort) if value.effort is not None else None,
    )


def _section_content(body: str, heading: str) -> str | None:
    matches = list(_HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1).strip() != heading:
            continue
        start = match.end()
        if start < len(body) and body[start] == "\n":
            start += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end]
    return None


def _latest_section_content(body: str, heading: str) -> str | None:
    """Return the last matching section body, or None if absent."""
    matches = list(_HEADING_RE.finditer(body))
    latest: str | None = None
    for index, match in enumerate(matches):
        if match.group(1).strip() != heading:
            continue
        start = match.end()
        if start < len(body) and body[start] == "\n":
            start += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        latest = body[start:end]
    return latest
