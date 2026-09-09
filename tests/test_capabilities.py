from __future__ import annotations

import subprocess

import pytest

from devflow.capabilities import ProviderCapabilities, discover_provider
from devflow.cli import _cmd_models


def test_discover_claude_options_from_local_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CLAUDE_CMD", "claude")
    monkeypatch.setattr("devflow.capabilities.shutil.which", lambda _name: "claude")
    help_text = """
      --effort <level>  Effort level (low, medium, high, xhigh, max)
      --model <model>   Alias ('fable', 'opus', or 'sonnet') or full name.
      --verbose         More output.
    """
    monkeypatch.setattr(
        "devflow.capabilities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=help_text, stderr=""
        ),
    )
    result = discover_provider("claude")
    assert result.configured is True
    assert result.available is True
    assert result.models == ("fable", "opus", "sonnet")
    assert result.efforts == ("low", "medium", "high", "xhigh", "max")
    assert result.supports_model_override is True
    assert result.supports_effort_override is True


def test_discover_codex_support_without_inventing_model_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVFLOW_CODEX_CMD", "codex")
    monkeypatch.setattr("devflow.capabilities.shutil.which", lambda _name: "codex")
    help_text = """
      -c, --config <key=value>  Override a configuration value.
      -m, --model <MODEL>       Model the agent should use.
    """
    monkeypatch.setattr(
        "devflow.capabilities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=help_text, stderr=""
        ),
    )
    result = discover_provider("codex")
    assert result.models == ()
    assert result.efforts == ("low", "medium", "high")
    assert result.supports_model_override is True
    assert result.supports_effort_override is True
    assert result.diagnostic == "model override supported; model catalogue unavailable"


def test_unconfigured_provider_does_not_run_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVFLOW_CODEX_CMD", raising=False)
    called = False

    def unexpected(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("must not run")

    monkeypatch.setattr("devflow.capabilities.subprocess.run", unexpected)
    result = discover_provider("codex")
    assert result.configured is False
    assert result.available is False
    assert result.diagnostic == "set DEVFLOW_CODEX_CMD"
    assert called is False


def test_models_command_reports_capabilities(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    capabilities = ProviderCapabilities(
        provider="claude",
        command="claude",
        configured=True,
        available=True,
        models=("opus", "sonnet"),
        efforts=("low", "high"),
        supports_model_override=True,
        supports_effort_override=True,
    )
    monkeypatch.setattr("devflow.cli.discover_all", lambda: (capabilities,))
    assert _cmd_models(refresh=True) == 0
    output = capsys.readouterr().out
    assert "CLAUDE" in output
    assert "configured  yes" in output
    assert "opus" in output
    assert "high" in output
