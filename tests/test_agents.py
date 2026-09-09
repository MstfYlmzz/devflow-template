from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from devflow.agents import (
    AgentMode,
    AgentStatus,
    _build_command,
    _defined_modes,
    run,
    run_with_retry,
    terminate_tree,
)


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


def test_agent_mode_has_exactly_three_values() -> None:
    assert {member.name for member in AgentMode} == {"READ_ONLY", "EDIT", "REVIEW"}
    assert len(AgentMode) == 3


def test_run_requires_mode(tmp_path: Path) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    with pytest.raises(TypeError):
        run("cursor", prompt, worktree)  # type: ignore[call-arg]


def test_run_exit_zero_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(tmp_path, "ok.py", "print('hello from agent')\n"),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
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
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
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
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
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
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.BLOCKED
    assert result.output == ""
    assert result.exit_code == 1
    assert result.detail == "agent exited with code 1\nsomething went boom"


def test_failure_diagnostic_is_redacted_and_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    secret = "sk-" + ("a" * 48)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "secret.py",
            (
                "import sys\n"
                "sys.stderr.write("
                f"'authentication failed {secret}\\n' + 'word ' * 1000"
                ")\n"
                "sys.exit(7)\n"
            ),
        ),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.BLOCKED
    assert result.output == ""
    assert result.exit_code == 7
    assert result.detail is not None
    assert secret not in result.detail
    assert "[REDACTED]" in result.detail
    assert result.detail.startswith("agent exited with code 7")
    assert result.detail.endswith("...[truncated]...")


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
    result = run(
        "cursor",
        prompt,
        worktree,
        AgentMode.READ_ONLY,
        timeout_minutes=0.05,
    )
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
        run("cursor", tmp_path / "missing.md", worktree, AgentMode.READ_ONLY)


def test_run_missing_worktree_raises(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        run("cursor", prompt, tmp_path / "no-such-dir", AgentMode.READ_ONLY)


def test_run_unknown_agent_raises(tmp_path: Path) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    with pytest.raises(ValueError):
        run("nope", prompt, worktree, AgentMode.READ_ONLY)


def test_run_unconfigured_command_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    monkeypatch.delenv("DEVFLOW_CURSOR_CMD", raising=False)
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
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
        AgentMode.READ_ONLY,
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
        AgentMode.READ_ONLY,
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
        AgentMode.READ_ONLY,
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
        AgentMode.READ_ONLY,
        sleep_fn=sleeps.append,
    )
    assert result.status is AgentStatus.OK
    assert sleeps == []


def test_claude_read_only_command_omits_edit_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CLAUDE_CMD", "claude")
    argv = _build_command("claude", AgentMode.READ_ONLY, "fix the bug")
    assert argv is not None
    assert "--allowedTools" in argv
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Edit" not in tools
    assert "Write" not in tools


def test_claude_edit_command_includes_edit_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CLAUDE_CMD", "claude")
    argv = _build_command("claude", AgentMode.EDIT, "fix the bug")
    assert argv is not None
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Edit" in tools
    assert "Write" in tools
    assert "acceptEdits" in argv


def test_cursor_command_has_no_mode_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CURSOR_CMD", "cursor-agent")
    for mode in AgentMode:
        argv = _build_command("cursor", mode, "fix the bug")
        assert argv == ["cursor-agent"]


def test_codex_read_only_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVFLOW_CODEX_CMD", "codex")
    prompt = "inspect the tree"
    argv = _build_command("codex", AgentMode.READ_ONLY, prompt)
    assert argv == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--color",
        "never",
        prompt,
    ]
    assert "--approve-for-me" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "danger-full-access" not in argv


def test_codex_edit_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVFLOW_CODEX_CMD", "codex")
    prompt = "fix the bug"
    argv = _build_command("codex", AgentMode.EDIT, prompt)
    assert argv == [
        "codex",
        "exec",
        "--approve-for-me",
        "--ephemeral",
        "--color",
        "never",
        prompt,
    ]
    # Codex 0.153.4 rejects --sandbox together with --approve-for-me;
    # --approve-for-me already applies workspace-write.
    assert "--sandbox" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "danger-full-access" not in argv


def test_codex_defined_modes() -> None:
    assert _defined_modes("codex") == (AgentMode.READ_ONLY, AgentMode.EDIT)
    assert AgentMode.REVIEW not in _defined_modes("codex")


def test_codex_review_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVFLOW_CODEX_CMD", "codex")
    with pytest.raises(ValueError, match="codex does not support mode review"):
        _build_command("codex", AgentMode.REVIEW, "review this")


def test_claude_defined_modes_unchanged() -> None:
    assert _defined_modes("claude") == (
        AgentMode.READ_ONLY,
        AgentMode.EDIT,
        AgentMode.REVIEW,
    )


def test_claude_review_matches_edit_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVFLOW_CLAUDE_CMD", "claude")
    edit = _build_command("claude", AgentMode.EDIT, "fix the bug")
    review = _build_command("claude", AgentMode.REVIEW, "fix the bug")
    assert edit == review
    assert edit is not None
    assert "acceptEdits" in edit


def test_codex_argv_never_includes_dangerous_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CODEX_CMD", "codex")
    for mode in (AgentMode.READ_ONLY, AgentMode.EDIT):
        argv = _build_command("codex", mode, "prompt")
        assert argv is not None
        joined = " ".join(argv)
        assert "--dangerously-bypass-approvals-and-sandbox" not in joined
        assert "danger-full-access" not in joined


def test_dangerously_skip_permissions_absent() -> None:
    needle = "--" + "dangerously-skip-permissions"
    root = Path(__file__).resolve().parents[1]
    skip_dirs = {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
    hits: list[str] = []
    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text:
            hits.append(path.as_posix())
    assert hits == []


def test_terminate_tree_kills_sleeping_process() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        terminate_tree(proc.pid)
        # POSIX keeps a killed child as a zombie until the parent wait()s;
        # is_process_alive would still report it live. Lock checks call that
        # helper on an unrelated pid, so they do not hit this parent/zombie
        # case. Popen.returncode is the right signal for this test.
        proc.wait(timeout=10)
        assert proc.returncode is not None
    finally:
        proc.wait(timeout=5)


def test_successful_output_is_stdout_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "split.py",
            (
                "import sys\n"
                "sys.stdout.buffer.write(b'FINAL ANSWER\\n')\n"
                "sys.stderr.buffer.write(b'OpenAI Codex...\\n')\n"
                "sys.stderr.buffer.write(b'gh pr merge\\n')\n"
                "sys.stderr.buffer.write(b'git push\\n')\n"
            ),
        ),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.OK
    assert result.output == "FINAL ANSWER\n"
    assert "OpenAI Codex" not in result.output
    assert "gh pr merge" not in result.output
    assert "git push" not in result.output


def test_failure_classifies_stderr_not_merged_into_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "fail.py",
            (
                "import sys\n"
                "sys.stdout.buffer.write(b'partial\\n')\n"
                "sys.stderr.buffer.write(b'rate limit exceeded\\n')\n"
                "sys.exit(1)\n"
            ),
        ),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.RETRY
    assert result.output == "partial\n"
    assert result.detail is not None
    assert "rate limit" in result.detail.casefold()


def test_stderr_activity_streams_before_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    secret = "sk-" + ("z" * 48)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "stream.py",
            (
                "import sys, time\n"
                f"sys.stderr.write('working {secret}\\n')\n"
                "sys.stderr.flush()\n"
                "time.sleep(0.3)\n"
                "sys.stderr.write('done\\n')\n"
                "sys.stdout.write('FINAL\\n')\n"
            ),
        ),
    )
    activity: list[str] = []
    first = threading.Event()
    result: list[object] = []

    def on_activity(line: str) -> None:
        activity.append(line)
        first.set()

    worker = threading.Thread(
        target=lambda: result.append(
            run(
                "cursor",
                prompt,
                worktree,
                AgentMode.READ_ONLY,
                on_activity=on_activity,
            )
        )
    )
    worker.start()
    assert first.wait(timeout=2)
    assert worker.is_alive()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert secret not in "\n".join(activity)
    assert "[REDACTED]" in activity[0]
    assert activity[-1] == "done"
    assert len(result) == 1
    agent_result = result[0]
    assert getattr(agent_result, "status") is AgentStatus.OK
    assert getattr(agent_result, "output").splitlines() == ["FINAL"]


def test_run_preserves_utf8_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    text = "önce birkaç kez\ndeğerlendir\ngenişletildi\nTRIAGE → PLAN_APPROVAL\n"
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "utf8.py",
            (
                "import sys\n"
                f"sys.stdout.buffer.write({text.encode('utf-8')!r})\n"
                "sys.stdout.buffer.flush()\n"
            ),
        ),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.OK
    assert result.output == text
    assert "Ã¶" not in result.output
    assert "Ä±" not in result.output


def test_invalid_utf8_agent_output_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(
        monkeypatch,
        _script(
            tmp_path,
            "badbytes.py",
            (
                "import sys\n"
                "sys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
                "sys.stdout.buffer.flush()\n"
            ),
        ),
    )
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.BLOCKED
    assert result.output == ""
    assert result.detail == "agent output is not valid UTF-8"


def test_unicode_decode_error_from_communicate_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt, worktree = _prompt_and_tree(tmp_path)
    _set_cursor(monkeypatch, _script(tmp_path, "noop.py", "print('x')\n"))

    class _FakeProc:
        pid = 0
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"\xff\xfe", b""

    monkeypatch.setattr("devflow.agents.subprocess.Popen", lambda *a, **k: _FakeProc())
    result = run("cursor", prompt, worktree, AgentMode.READ_ONLY)
    assert result.status is AgentStatus.BLOCKED
    assert result.detail == "agent output is not valid UTF-8"
