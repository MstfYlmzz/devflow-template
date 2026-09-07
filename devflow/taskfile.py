"""Read and write `.devflow/tasks/<id>.md` context files.

Frontmatter is mutable. The markdown body is append-only: existing
lines are never edited or deleted. Outcomes such as risk and
implementer are not stored; they are recomputed by policy.decide()
on each read.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

import yaml

from devflow.policy import (
    ArchitectureImpact,
    Complexity,
    EpicProposal,
    PolicyResult,
    Risk,
    TriageSignals,
)

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
    (r"[A-Za-z0-9+/_=-]{40,}", "[REDACTED]"),
)

_HEADING_RE = re.compile(r"^## ([^#].*?)\s*$", re.MULTILINE)
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


def redact(text: str) -> str:
    result = text
    for pattern, replacement in _COMPILED_SECRETS:
        result = pattern.sub(replacement, result)
    return result


def body_sections(tf: TaskFile) -> list[str]:
    return [match.group(1).strip() for match in _HEADING_RE.finditer(tf.body)]


def decision_inputs(
    tf: TaskFile,
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
    floor = PolicyResult(
        risk_floor=fm.floor_risk,
        complexity_hint=None,
        architecture_block=False,
        matched_rules=list(fm.floor_matched),
    )
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
    section = _section_content(tf.body, "Doc impact")
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


def _yaml_value(value: object) -> object:
    if isinstance(value, (Risk, Complexity, ArchitectureImpact)):
        return value.value
    if isinstance(value, TriageSignals):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_yaml_value(item) for item in value]
    return value


def _frontmatter_dict(fm: TaskFrontmatter) -> dict[str, object]:
    data: dict[str, object] = {}
    for item in dataclasses.fields(fm):
        data[item.name] = _yaml_value(getattr(fm, item.name))
    return data


def _write(path: Path, fm: TaskFrontmatter, body: str) -> None:
    dumped = yaml.safe_dump(
        _frontmatter_dict(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    text = f"---\n{dumped}---\n{body}"
    path.write_text(text, encoding="utf-8")


def _redact_str_list(values: list[str]) -> list[str]:
    return [redact(item) for item in values]


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


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
