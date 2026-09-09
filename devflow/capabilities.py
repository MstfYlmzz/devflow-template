from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from devflow.agents import COMMANDS, _resolve_command
from devflow.authority import sanitized_env

_CODEX_EFFORTS = ("low", "medium", "high")
_KNOWN_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    command: str | None
    configured: bool
    available: bool
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    supports_model_override: bool
    supports_effort_override: bool
    diagnostic: str | None = None


def discover_provider(provider: str) -> ProviderCapabilities:
    if provider not in COMMANDS:
        raise ValueError(f"unknown provider: {provider}")
    argv = _resolve_command(provider)
    if argv is None:
        return ProviderCapabilities(
            provider=provider,
            command=None,
            configured=False,
            available=False,
            models=(),
            efforts=(),
            supports_model_override=False,
            supports_effort_override=False,
            diagnostic=f"set {COMMANDS[provider]}",
        )
    command = argv[0]
    if shutil.which(command) is None:
        return ProviderCapabilities(
            provider=provider,
            command=command,
            configured=True,
            available=False,
            models=(),
            efforts=(),
            supports_model_override=False,
            supports_effort_override=False,
            diagnostic="command not found in PATH",
        )
    help_argv = [*argv, *(["exec", "--help"] if provider == "codex" else ["--help"])]
    try:
        result = subprocess.run(
            help_argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=sanitized_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderCapabilities(
            provider=provider,
            command=command,
            configured=True,
            available=True,
            models=(),
            efforts=(),
            supports_model_override=False,
            supports_effort_override=False,
            diagnostic=f"capability discovery unavailable: {type(exc).__name__}",
        )
    help_text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return ProviderCapabilities(
            provider=provider,
            command=command,
            configured=True,
            available=True,
            models=(),
            efforts=(),
            supports_model_override=False,
            supports_effort_override=False,
            diagnostic=f"help exited with code {result.returncode}",
        )
    model_block = _option_block(help_text, "--model")
    supports_model = bool(model_block)
    if provider == "claude":
        models = _quoted_values(model_block)
        effort_block = _option_block(help_text, "--effort")
        efforts = tuple(item for item in _KNOWN_EFFORTS if item in effort_block)
        supports_effort = bool(effort_block and efforts)
    elif provider == "codex":
        models = ()
        config_block = _option_block(help_text, "--config")
        efforts = _CODEX_EFFORTS if config_block else ()
        supports_effort = bool(config_block)
    else:
        models = ()
        efforts = ()
        supports_effort = False
    diagnostic = None
    if supports_model and not models:
        diagnostic = "model override supported; model catalogue unavailable"
    return ProviderCapabilities(
        provider=provider,
        command=command,
        configured=True,
        available=True,
        models=models,
        efforts=efforts,
        supports_model_override=supports_model,
        supports_effort_override=supports_effort,
        diagnostic=diagnostic,
    )


def discover_all() -> tuple[ProviderCapabilities, ...]:
    return tuple(discover_provider(provider) for provider in COMMANDS)


def _option_block(help_text: str, option: str) -> str:
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if option not in line:
            continue
        block = [line.strip()]
        for continuation in lines[index + 1 :]:
            stripped = continuation.strip()
            if stripped.startswith("-"):
                break
            if stripped:
                block.append(stripped)
        return " ".join(block).casefold()
    return ""


def _quoted_values(block: str) -> tuple[str, ...]:
    import re

    found = re.findall(r"['\"]([a-z0-9][a-z0-9._-]*)['\"]", block)
    return tuple(dict.fromkeys(found))
