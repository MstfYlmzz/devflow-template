from __future__ import annotations

import pytest

from devflow.policy import Complexity
from devflow.runtime import RuntimeChoice, RuntimeSelection, recommended_effort


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
