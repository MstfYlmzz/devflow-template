from __future__ import annotations

import threading

import pytest

from devflow.capabilities import ProviderCapabilities
from devflow.cli import _cmd_start, _interactive_runtime_selector
from devflow.policy import Complexity
from devflow.runner import StartResult, _runtime_heartbeat
from devflow.runtime import RuntimeChoice, RuntimeSelection, recommended_effort
from devflow.states import State


def test_recommended_effort_uses_complexity_not_risk() -> None:
    assert recommended_effort(Complexity.LOW, "triage") == "low"
    assert recommended_effort(Complexity.LOW, "implementer") == "medium"
    assert recommended_effort(Complexity.MEDIUM, "triage") == "medium"
    assert recommended_effort(Complexity.MEDIUM, "reviewer") == "high"
    assert recommended_effort(Complexity.HIGH, "implementer") == "high"


def test_runtime_selection_is_role_based_without_provider_authority() -> None:
    choice = RuntimeChoice(model="model-x", effort="high")
    selection = RuntimeSelection(implementer=choice)
    assert selection.for_role("implementer") == choice
    assert not hasattr(choice, "provider")
    with pytest.raises(ValueError, match="unknown runtime role"):
        selection.for_role("merger")


def test_interactive_configuration_never_offers_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        "triage": "codex",
        "implementer": "codex",
        "reviewer": "claude",
    }
    capabilities = {
        "codex": ProviderCapabilities(
            "codex",
            "codex",
            True,
            True,
            ("gpt-fast",),
            ("low", "medium", "high"),
            True,
            True,
        ),
        "claude": ProviderCapabilities(
            "claude",
            "claude",
            True,
            True,
            ("sonnet", "opus"),
            ("low", "medium", "high", "xhigh"),
            True,
            True,
        ),
    }
    recommended = RuntimeSelection(
        triage=RuntimeChoice(effort="medium"),
        implementer=RuntimeChoice(effort="high"),
        reviewer=RuntimeChoice(effort="high"),
    )
    answers = iter(["c", "1", "low", "gpt-code", "high", "2", "xhigh"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    selected = _interactive_runtime_selector(
        object(),  # type: ignore[arg-type]
        providers,
        recommended,
        capabilities,
    )
    assert selected.triage == RuntimeChoice(model="gpt-fast", effort="low")
    assert selected.implementer == RuntimeChoice(model="gpt-code", effort="high")
    assert selected.reviewer == RuntimeChoice(model="opus", effort="xhigh")


def test_noninteractive_start_never_reads_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("devflow.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("devflow.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("input called")),
    )
    captured: dict[str, object] = {}

    def fake_start(*args: object, **kwargs: object) -> StartResult:
        captured.update(kwargs)
        return StartResult(1, State.READY_TO_MERGE, None, True, None)

    monkeypatch.setattr("devflow.cli.repo_root", lambda: object())
    monkeypatch.setattr("devflow.cli.start", fake_start)
    code = _cmd_start(
        task_id=1,
        risk_arg=None,
        skip_review=False,
        review=None,
        reason=None,
        dry_run=False,
    )
    assert code == 0
    assert captured["runtime_selector"] is None


def test_runtime_heartbeat_reports_elapsed_while_agent_is_silent() -> None:
    activity: list[str] = []
    stop = threading.Event()
    emitted = threading.Event()

    def record(line: str) -> None:
        activity.append(line)
        emitted.set()

    thread = threading.Thread(
        target=_runtime_heartbeat,
        args=(record, stop, 0.01),
    )
    thread.start()
    assert emitted.wait(timeout=1)
    stop.set()
    thread.join(timeout=1)
    assert activity
    assert all(item.startswith("working · elapsed 00:") for item in activity)
