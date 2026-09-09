from __future__ import annotations

from dataclasses import dataclass

from devflow.policy import Complexity

RUNTIME_ROLES = ("triage", "implementer", "reviewer")


@dataclass(frozen=True)
class RuntimeChoice:
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class RuntimeSelection:
    triage: RuntimeChoice = RuntimeChoice()
    implementer: RuntimeChoice = RuntimeChoice()
    reviewer: RuntimeChoice = RuntimeChoice()

    def for_role(self, role: str) -> RuntimeChoice:
        if role not in RUNTIME_ROLES:
            raise ValueError(f"unknown runtime role: {role}")
        return getattr(self, role)  # type: ignore[no-any-return]


def recommended_effort(complexity: Complexity, role: str) -> str:
    if role not in RUNTIME_ROLES:
        raise ValueError(f"unknown runtime role: {role}")
    if complexity is Complexity.LOW:
        return "low" if role == "triage" else "medium"
    if complexity is Complexity.MEDIUM:
        return "medium" if role == "triage" else "high"
    return "high"
