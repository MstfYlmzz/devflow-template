from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from devflow.agents import AgentStatus, run, run_with_retry


def _cmd_string(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _set_cursor(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setenv("DEVFLOW_CURSOR_CMD", _cmd_string(args))


def _prompt_and_tree(tmp_path: Path) -> tuple[Path, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    worktree = tmp_path / "work"
    worktree.mkdir()
    return prompt, worktree


def _script(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def test_agent_status_has_exactly_three_values() -> None:
    assert {member.name for member in AgentStatus} == {"OK", "RETRY", "BLOCKED"}
    assert len(AgentStatus) == 3


def test_run_exit_zero_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(tmp_path, "ok.py", "print('hello from agent')\n"),
    )
    result = run("cursor", prompt, worktree)
    assert result.status is AgentStatus.OK
    assert "hello from agent" in result.output
    assert result.duration_seconds > 0


def test_run_rate_limit_is_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "retry.py",
            "import sys\nsys.stderr.write('rate limit exceeded')\nsys.exit(1)\n",
        ),
    )
    result = run("cursor", prompt, worktree)
    assert result.status is AgentStatus.RETRY


def test_run_invalid_api_key_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "auth.py",
            "import sys\nsys.stderr.write('invalid api key')\nsys.exit(1)\n",
        ),
    )
    result = run("cursor", prompt, worktree)
    assert result.status is AgentStatus.BLOCKED


def test_run_unknown_stderr_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "boom.py",
            "import sys\nsys.stderr.write('something went boom')\nsys.exit(1)\n",
        ),
    )
    result = run("cursor", prompt, worktree)
    assert result.status is AgentStatus.BLOCKED


def test_run_timeout_kills_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    script = tmp_path / "sleepy.py"
    script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
        "Path(sys.argv[2]).write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    pid_file = worktree / "pid.txt"
    done_file = worktree / "done.txt"
    _set_cursor(
        monkeypatch,
        [sys.executable, str(script), str(pid_file), str(done_file)],
    )
    result = run("cursor", prompt, worktree, timeout_minutes=0.05)
    assert result.status is AgentStatus.BLOCKED
    assert result.duration_seconds < 10
    assert not done_file.exists()
    if pid_file.exists() and os.name != "nt":
        pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(OSError):
            os.kill(pid, 0)


def test_run_missing_prompt_raises(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()
    with pytest.raises(ValueError):
        run("cursor", tmp_path / "missing.md", worktree)


def test_run_missing_worktree_raises(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        run("cursor", prompt, tmp_path / "no-such-dir")


def test_run_unknown_agent_raises(tmp_path: Path) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    with pytest.raises(ValueError):
        run("nope", prompt, worktree)


def test_run_unconfigured_command_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    monkeypatch.delenv("DEVFLOW_CURSOR_CMD", raising=False)
    result = run("cursor", prompt, worktree)
    assert result.status is AgentStatus.BLOCKED
    assert result.detail is not None
    assert "cursor command not configured (set DEVFLOW_CURSOR_CMD)" in result.detail


def test_retry_then_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    script = worktree / "agent.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "n = Path('count.txt')\n"
        "count = int(n.read_text()) if n.exists() else 0\n"
        "count += 1\n"
        "n.write_text(str(count))\n"
        "if count < 3:\n"
        "    sys.stderr.write('rate limit exceeded')\n"
        "    sys.exit(1)\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    _set_cursor(monkeypatch, [sys.executable, str(script)])
    sleeps: list[float] = []
    result = run_with_retry(
        "cursor",
        prompt,
        worktree,
        sleep_fn=sleeps.append,
    )
    assert result.status is AgentStatus.OK
    assert sleeps == [30, 120]


def test_retry_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "always_retry.py",
            "import sys\nsys.stderr.write('rate limit exceeded')\nsys.exit(1)\n",
        ),
    )
    sleeps: list[float] = []
    result = run_with_retry(
        "cursor",
        prompt,
        worktree,
        sleep_fn=sleeps.append,
    )
    assert result.status is AgentStatus.BLOCKED
    assert result.detail is not None
    assert "retry exhausted" in result.detail
    assert sleeps == [30, 120]


def test_retry_stops_on_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "blocked.py",
            "import sys\nsys.stderr.write('invalid api key')\nsys.exit(1)\n",
        ),
    )
    sleeps: list[float] = []
    result = run_with_retry(
        "cursor",
        prompt,
        worktree,
        sleep_fn=sleeps.append,
    )
    assert result.status is AgentStatus.BLOCKED
    assert sleeps == []


def test_retry_stops_on_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(monkeypatch, _script(tmp_path, "once.py", "print('ok')\n"))
    sleeps: list[float] = []
    result = run_with_retry(
        "cursor",
        prompt,
        worktree,
        sleep_fn=sleeps.append,
    )
    assert result.status is AgentStatus.OK
    assert sleeps == []
