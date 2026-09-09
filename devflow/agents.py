from __future__ import annotations

import enum
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devflow.authority import sanitized_env
from devflow.lock import is_process_alive
from devflow.taskfile import redact

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

_CLAUDE_READ_TOOLS = "Read,Grep,Glob"
_CLAUDE_EDIT_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"


class AgentStatus(enum.Enum):
    OK = "ok"
    RETRY = "retry"
    BLOCKED = "blocked"


class AgentMode(enum.Enum):
    READ_ONLY = "read_only"
    EDIT = "edit"
    REVIEW = "review"


@dataclass
class AgentResult:
    status: AgentStatus
    output: str
    detail: str | None
    duration_seconds: float
    exit_code: int | None = None


def _split_command(raw: str) -> list[str]:
    return shlex.split(raw, posix=os.name != "nt")


def _resolve_command(agent: str) -> list[str] | None:
    env_name = COMMANDS[agent]
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    return _split_command(raw)


def _claude_mode_flags(mode: AgentMode, prompt: str) -> list[str]:
    if mode is AgentMode.READ_ONLY:
        return ["-p", prompt, "--allowedTools", _CLAUDE_READ_TOOLS]
    # REVIEW uses the same flags as EDIT. Isolation is a separate worktree,
    # not a tighter Claude permission set.
    return [
        "-p",
        prompt,
        "--allowedTools",
        _CLAUDE_EDIT_TOOLS,
        "--permission-mode",
        "acceptEdits",
    ]


def _codex_mode_flags(mode: AgentMode, prompt: str) -> list[str]:
    """Build Codex CLI flags for non-interactive implementer modes.

    Codex is an implementer only (READ_ONLY / EDIT). REVIEW is fail-closed —
    Claude remains the reviewer. Never use danger-full-access or
    --dangerously-bypass-approvals-and-sandbox.

    EDIT uses --approve-for-me alone: Codex 0.153.4 rejects combining it with
    --sandbox, and --approve-for-me already applies the workspace-write sandbox
    while auto-reviewing approval prompts (needed for non-interactive runs).
    """
    if mode is AgentMode.READ_ONLY:
        return [
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--color",
            "never",
            prompt,
        ]
    if mode is AgentMode.EDIT:
        return [
            "exec",
            "--approve-for-me",
            "--ephemeral",
            "--color",
            "never",
            prompt,
        ]
    raise ValueError(
        f"codex does not support mode {mode.value} "
        "(supported: read_only, edit; reviewer remains claude)"
    )


def _mode_flags(agent: str, mode: AgentMode, prompt: str) -> list[str]:
    if agent == "claude":
        return _claude_mode_flags(mode, prompt)
    if agent == "codex":
        return _codex_mode_flags(mode, prompt)
    # TODO: cursor mode flags are not known yet.
    return []


def _defined_modes(agent: str) -> tuple[AgentMode, ...]:
    if agent not in COMMANDS:
        raise ValueError(f"unknown agent: {agent}")
    if agent == "claude":
        return (AgentMode.READ_ONLY, AgentMode.EDIT, AgentMode.REVIEW)
    if agent == "codex":
        return (AgentMode.READ_ONLY, AgentMode.EDIT)
    return ()


def _build_command(
    agent: str,
    mode: AgentMode,
    prompt: str,
    model: str | None = None,
    effort: str | None = None,
) -> list[str] | None:
    argv = _resolve_command(agent)
    if argv is None:
        return None
    mode_flags = _mode_flags(agent, mode, prompt)
    runtime_flags = _runtime_flags(agent, model, effort)
    if agent == "codex":
        # Codex runtime flags belong to `exec`, before mode-specific options.
        return [*argv, mode_flags[0], *runtime_flags, *mode_flags[1:]]
    return [*argv, *runtime_flags, *mode_flags]


def _runtime_flags(agent: str, model: str | None, effort: str | None) -> list[str]:
    if model is not None and not model.strip():
        raise ValueError("model must not be empty")
    if effort is not None and not effort.strip():
        raise ValueError("effort must not be empty")
    if agent == "codex":
        if effort is not None and effort not in {"low", "medium", "high"}:
            raise ValueError(f"unsupported codex effort: {effort}")
        flags = ["--model", model] if model is not None else []
        if effort is not None:
            flags.extend(["--config", f'model_reasoning_effort="{effort}"'])
        return flags
    if agent == "claude":
        if effort is not None and effort not in {
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError(f"unsupported claude effort: {effort}")
        flags = ["--model", model] if model is not None else []
        if effort is not None:
            flags.extend(["--effort", effort])
        return flags
    if model is not None or effort is not None:
        raise ValueError(f"{agent} does not support runtime overrides")
    return []


def terminate_tree(pid: int, grace_seconds: float = 5) -> None:
    """Terminate a process and its descendants. SIGTERM/taskkill, then force."""
    if not is_process_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        if _wait_until_dead(pid, grace_seconds):
            return
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        _wait_until_dead(pid, 1)
        return
    pids = [pid, *_descendant_pids(pid)]
    for child in pids:
        try:
            os.kill(child, signal.SIGTERM)
        except OSError:
            pass
    if _wait_until_dead(pid, grace_seconds):
        return
    pids = [pid, *_descendant_pids(pid)]
    sigkill = getattr(signal, "SIGKILL", 9)
    for child in pids:
        try:
            os.kill(child, sigkill)
        except OSError:
            pass
    _wait_until_dead(pid, 1)


def _wait_until_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return not is_process_alive(pid)


def _descendant_pids(pid: int) -> list[int]:
    found: list[int] = []
    for child in _child_pids(pid):
        found.append(child)
        found.extend(_descendant_pids(child))
    return found


def _child_pids(pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return _proc_children(pid)
    if result.returncode != 0:
        return _proc_children(pid)
    return [int(item) for item in result.stdout.split() if item.isdigit()]


def _proc_children(pid: int) -> list[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    children: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        stat_path = entry / "stat"
        try:
            text = stat_path.read_text(encoding="utf-8")
        except OSError:
            continue
        close = text.rfind(")")
        if close == -1:
            continue
        fields = text[close + 1 :].split()
        if len(fields) < 2:
            continue
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        if ppid == pid:
            children.append(int(entry.name))
    return children


def classify_failure(exit_code: int, stderr: str) -> tuple[AgentStatus, str]:
    _ = exit_code
    lowered = stderr.casefold()
    for pattern in RETRY_PATTERNS:
        if pattern.casefold() in lowered:
            return AgentStatus.RETRY, _safe_failure_detail(exit_code, stderr)
    return AgentStatus.BLOCKED, _safe_failure_detail(exit_code, stderr)


def _safe_failure_detail(exit_code: int, stderr: str, limit: int = 2000) -> str:
    """Return a bounded, secret-safe process diagnostic for terminal/journal use."""
    cleaned = redact(stderr).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n...[truncated]..."
    prefix = f"agent exited with code {exit_code}"
    return f"{prefix}\n{cleaned}" if cleaned else prefix


def run(
    agent: str,
    prompt_file: Path,
    worktree: Path,
    mode: AgentMode,
    timeout_minutes: float = 20,
    env: dict[str, str] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_activity: Callable[[str], None] | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> AgentResult:
    if agent not in COMMANDS:
        raise ValueError(f"unknown agent: {agent}")
    if not prompt_file.is_file():
        raise ValueError(f"prompt file not found: {prompt_file}")
    if not worktree.is_dir():
        raise ValueError(f"worktree is not a directory: {worktree}")

    started = time.monotonic()
    env_name = COMMANDS[agent]
    prompt = prompt_file.read_text(encoding="utf-8")
    argv = _build_command(agent, mode, prompt, model=model, effort=effort)
    if argv is None:
        return AgentResult(
            status=AgentStatus.BLOCKED,
            output="",
            detail=f"{agent} command not configured (set {env_name})",
            duration_seconds=time.monotonic() - started,
            exit_code=None,
        )

    timeout_seconds = timeout_minutes * 60
    popen_kwargs: dict[str, object] = {
        "cwd": worktree,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": sanitized_env() if env is None else env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[call-overload]
    if on_spawn is not None:
        on_spawn(proc.pid)
    if on_activity is not None:
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        readers = [
            threading.Thread(
                target=_read_pipe,
                args=(proc.stdout, stdout_parts, None),
                daemon=True,
            ),
            threading.Thread(
                target=_read_pipe,
                args=(proc.stderr, stderr_parts, on_activity),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
    try:
        if on_activity is None:
            stdout_b, stderr_b = proc.communicate(timeout=timeout_seconds)
        else:
            proc.wait(timeout=timeout_seconds)
            for reader in readers:
                reader.join(timeout=1)
            stdout_b = b"".join(stdout_parts)
            stderr_b = b"".join(stderr_parts)
    except subprocess.TimeoutExpired:
        terminate_tree(proc.pid)
        try:
            if on_activity is None:
                stdout_b, stderr_b = proc.communicate(timeout=1)
            else:
                proc.wait(timeout=1)
                for reader in readers:
                    reader.join(timeout=1)
                stdout_b = b"".join(stdout_parts)
                stderr_b = b"".join(stderr_parts)
        except subprocess.TimeoutExpired:
            stdout_b, stderr_b = b"", b""
        if is_process_alive(proc.pid):
            term_detail = "timeout; process tree still running after terminate"
        else:
            term_detail = "timeout; process tree terminated"
        stdout_text, stderr_text = _decode_agent_pipes(stdout_b, stderr_b)
        if stdout_text is None:
            return AgentResult(
                status=AgentStatus.BLOCKED,
                output="",
                detail="agent output is not valid UTF-8",
                duration_seconds=time.monotonic() - started,
                exit_code=proc.returncode,
            )
        return AgentResult(
            status=AgentStatus.BLOCKED,
            output=stdout_text,
            detail=term_detail,
            duration_seconds=time.monotonic() - started,
            exit_code=proc.returncode,
        )
    except KeyboardInterrupt:
        terminate_tree(proc.pid)
        proc.wait(timeout=1)
        raise

    decoded = _decode_agent_pipes(stdout_b, stderr_b)
    stdout_text, stderr_text = decoded
    if stdout_text is None or stderr_text is None:
        return AgentResult(
            status=AgentStatus.BLOCKED,
            output="",
            detail="agent output is not valid UTF-8",
            duration_seconds=time.monotonic() - started,
            exit_code=proc.returncode,
        )
    duration = time.monotonic() - started
    if proc.returncode == 0:
        return AgentResult(
            status=AgentStatus.OK,
            output=stdout_text,
            detail=None,
            duration_seconds=duration,
            exit_code=0,
        )
    status, detail = classify_failure(proc.returncode or 1, stderr_text)
    return AgentResult(
        status=status,
        output=stdout_text,
        detail=detail,
        duration_seconds=duration,
        exit_code=proc.returncode,
    )


def _read_pipe(
    pipe: object,
    parts: list[bytes],
    on_activity: Callable[[str], None] | None,
) -> None:
    if pipe is None or not hasattr(pipe, "readline"):
        return
    while True:
        chunk = pipe.readline()
        if not chunk:
            return
        if isinstance(chunk, str):
            raw = chunk.encode("utf-8")
        else:
            raw = bytes(chunk)
        parts.append(raw)
        if on_activity is None:
            continue
        safe = redact(raw.decode("utf-8", errors="replace")).strip()
        if safe:
            on_activity(safe[:1000])


def _decode_agent_pipes(
    stdout_b: bytes | None, stderr_b: bytes | None
) -> tuple[str | None, str | None]:
    """Decode agent pipes as UTF-8. Return ``(None, None)`` on decode failure."""
    try:
        stdout_text = (stdout_b or b"").decode("utf-8")
        stderr_text = (stderr_b or b"").decode("utf-8")
    except UnicodeDecodeError:
        return None, None
    return stdout_text, stderr_text


def run_with_retry(
    agent: str,
    prompt_file: Path,
    worktree: Path,
    mode: AgentMode,
    max_attempts: int = 3,
    timeout_minutes: float = 20,
    sleep_fn: Callable[[float], None] = time.sleep,
    model: str | None = None,
    effort: str | None = None,
) -> AgentResult:
    last: AgentResult | None = None
    for attempt in range(max_attempts):
        last = run(
            agent,
            prompt_file,
            worktree,
            mode,
            timeout_minutes=timeout_minutes,
            model=model,
            effort=effort,
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
        exit_code=last.exit_code,
    )
