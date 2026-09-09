from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SETUP = _ROOT / "scripts" / "setup-worktree"
_TEMPLATE_SETUP = _ROOT / "templates" / "project" / "scripts" / "setup-worktree"


def _git_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("bash not found on PATH")
    # Prefer the Git for Windows binary explicitly. A bare "bash" can resolve
    # to WSL under CreateProcess and break PATH / mount semantics.
    return str(Path(bash).resolve())


def _bash(
    *args: str,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_git_bash(), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_bin(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    return bin_dir


def _path_env(bin_dir: Path) -> dict[str, str]:
    """Build a PATH where fake interpreters are tried before host tools.

    Fake binaries are prepended, then the Bash directory (needed so the
    script can run). On Linux that Bash directory is often ``/usr/bin``,
    which may also contain a real host ``python`` / ``python3``. Tests that
    care about selection must therefore install explicit fake candidates
    for both names rather than assuming the host interpreters are hidden.

    On Windows, Git Bash receives PATH via CreateProcess using Windows
    ``;``-separated entries (not a POSIX ``:`` PATH).
    """
    env = os.environ.copy()
    bash = _git_bash()
    entries = [str(bin_dir.resolve()), str(Path(bash).parent)]
    env["PATH"] = os.pathsep.join(entries)
    return env


def _usable_python_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-c" ]]; then
          # Claim Python >= 3.11 for the setup-worktree probe.
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          dest="${3:-.venv}"
          mkdir -p "$dest/bin"
          printf '%s\\n' '#!/usr/bin/env bash' 'exit 0' > "$dest/bin/python"
          chmod +x "$dest/bin/python"
          exit 0
        fi
        echo "unexpected: $*" >&2
        exit 1
        """
    )


def _broken_python_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        # WindowsApps-style false positive: present on PATH, fails when run.
        echo "Python was not found" >&2
        exit 49
        """
    )


def _old_python_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-c" ]]; then
          # Fail the >=3.11 probe.
          exit 1
        fi
        exit 1
        """
    )


def _failing_venv_python_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-c" ]]; then
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          dest="${3:-.venv}"
          mkdir -p "$dest/lib64" "$dest/Scripts"
          printf 'partial\\n' > "$dest/pyvenv.cfg"
          echo "venv boom" >&2
          exit 37
        fi
        exit 1
        """
    )


def _install_script(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / "setup-worktree"
    script.write_text(
        _SETUP.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_setup_worktree_root_template_parity() -> None:
    assert _SETUP.read_text(encoding="utf-8") == _TEMPLATE_SETUP.read_text(
        encoding="utf-8"
    )
    text = _SETUP.read_text(encoding="utf-8")
    assert "for cand in python python3" in text
    assert "sys.version_info >= (3, 11)" in text
    assert "error: Python 3.11+ not found" in text
    assert "remove_venv_dir" in text
    assert '"$py" -m venv .venv || status=$?' in text


def test_prefers_working_python_over_broken_python3(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path)
    _write_exec(bin_dir / "python", _usable_python_stub())
    _write_exec(bin_dir / "python3", _broken_python_stub())
    _install_script(tmp_path)
    result = _bash(
        "scripts/setup-worktree",
        "--print-python",
        env=_path_env(bin_dir),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python"


def test_falls_back_to_working_python3(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path)
    # Explicit unusable `python` so a host /usr/bin/python cannot win first.
    _write_exec(bin_dir / "python", _broken_python_stub())
    _write_exec(bin_dir / "python3", _usable_python_stub())
    _install_script(tmp_path)
    result = _bash(
        "scripts/setup-worktree",
        "--print-python",
        env=_path_env(bin_dir),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3"


def test_errors_when_no_usable_python(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path)
    _write_exec(bin_dir / "python", _broken_python_stub())
    _write_exec(bin_dir / "python3", _broken_python_stub())
    _install_script(tmp_path)
    result = _bash(
        "scripts/setup-worktree",
        "--print-python",
        env=_path_env(bin_dir),
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "error: Python 3.11+ not found" in result.stderr


def test_rejects_python_below_3_11(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path)
    _write_exec(bin_dir / "python", _old_python_stub())
    _write_exec(bin_dir / "python3", _usable_python_stub())
    _install_script(tmp_path)
    result = _bash(
        "scripts/setup-worktree",
        "--print-python",
        env=_path_env(bin_dir),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3"


def test_partial_venv_cleaned_on_failure(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path)
    _write_exec(bin_dir / "python", _failing_venv_python_stub())
    _install_script(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0"\n',
        encoding="utf-8",
    )
    result = _bash("scripts/setup-worktree", env=_path_env(bin_dir), cwd=tmp_path)
    assert result.returncode == 37
    assert "failed to create .venv" in result.stderr
    assert not (tmp_path / ".venv").exists()
