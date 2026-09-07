from __future__ import annotations

import enum
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

COMMANDS: dict[str, str] = {
    "cursor": "DEVFLOW_CURSOR_CMD",
    "codex": "DEVFLOW_CODEX_CMD",
    "claude": "DEVFLOW_CLAUDE_CMD",
}

RETRY_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "usage limit",
    "429",
    "timeout of",
    "connection reset",
    "temporarily unavailable",
)

_RETRY_DELAYS: tuple[int, ...] = (30, 120)


class AgentStatus(enum.Enum):
    OK = "ok"
    RETRY = "retry"
    BLOCKED = "blocked"


@dataclass
class AgentResult:
    status: AgentStatus
    output: str
    detail: str | None
    duration_seconds: float


def _split_command(raw: str) -> list[str]:
    return shlex.split(raw, posix=os.name != "nt")


def _resolve_command(agent: str) -> list[str] | None:
    env_name = COMMANDS[agent]
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    return _split_command(raw)


def classify_failure(exit_code: int, stderr: str) -> tuple[AgentStatus, str]:
    _ = exit_code
    lowered = stderr.casefold()
    for pattern in RETRY_PATTERNS:
        if pattern.casefold() in lowered:
            return AgentStatus.RETRY, pattern
    return AgentStatus.BLOCKED, "blocked"


def run(
    agent: str,
    prompt_file: Path,
    worktree: Path,
    timeout_minutes: float = 20,
) -> AgentResult:
    if agent not in COMMANDS:
        raise ValueError(f"unknown agent: {agent}")
    if not prompt_file.is_file():
        raise ValueError(f"prompt file not found: {prompt_file}")
    if not worktree.is_dir():
        raise ValueError(f"worktree is not a directory: {worktree}")

    started = time.monotonic()
    env_name = COMMANDS[agent]
    argv = _resolve_command(agent)
    if argv is None:
        return AgentResult(
            status=AgentStatus.BLOCKED,
            output="",
            detail=f"{agent} command not configured (set {env_name})",
            duration_seconds=time.monotonic() - started,
        )

    timeout_seconds = timeout_minutes * 60
    try:
        completed = subprocess.run(
            argv,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if isinstance(exc.stdout, bytes):
            stdout = exc.stdout.decode("utf-8", errors="replace")
        if isinstance(exc.stderr, bytes):
            stderr = exc.stderr.decode("utf-8", errors="replace")
        return AgentResult(
            status=AgentStatus.BLOCKED,
            output=f"{stdout}{stderr}",
            detail="timeout",
            duration_seconds=time.monotonic() - started,
        )

    output = f"{completed.stdout}{completed.stderr}"
    duration = time.monotonic() - started
    if completed.returncode == 0:
        return AgentResult(
            status=AgentStatus.OK,
            output=output,
            detail=None,
            duration_seconds=duration,
        )
    status, detail = classify_failure(completed.returncode, completed.stderr)
    return AgentResult(
        status=status,
        output=output,
        detail=detail,
        duration_seconds=duration,
    )


def run_with_retry(
    agent: str,
    prompt_file: Path,
    worktree: Path,
    max_attempts: int = 3,
    timeout_minutes: float = 20,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> AgentResult:
    last: AgentResult | None = None
    for attempt in range(max_attempts):
        last = run(
            agent,
            prompt_file,
            worktree,
            timeout_minutes=timeout_minutes,
        )
        if last.status is not AgentStatus.RETRY:
            return last
        if attempt + 1 < max_attempts:
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            sleep_fn(delay)
    assert last is not None
    return AgentResult(
        status=AgentStatus.BLOCKED,
        output=last.output,
        detail=f"retry exhausted after {max_attempts} attempts: {last.detail}",
        duration_seconds=last.duration_seconds,
    )
