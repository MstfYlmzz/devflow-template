from __future__ import annotations

from pathlib import Path

_POLICY = (
    Path(__file__).resolve().parents[1] / "templates" / "project" / ".ai" / "policy.yml"
)
_TOP_LEVEL = ("floor", "no_bypass", "signal_floor", "routing")


def _top_level_keys(text: str) -> list[str]:
    keys: list[str] = []
    for raw in text.splitlines():
        if not raw or raw.startswith("#") or raw.startswith(" ") or raw.startswith("-"):
            continue
        if ":" not in raw:
            raise AssertionError(f"invalid top-level YAML line: {raw!r}")
        keys.append(raw.split(":", 1)[0])
    return keys


def test_policy_yml_is_valid_yaml_with_required_keys() -> None:
    text = _POLICY.read_text(encoding="utf-8")
    assert "\t" not in text
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        data = yaml.safe_load(text)
        assert list(data) == list(_TOP_LEVEL)
        return
    assert _top_level_keys(text) == list(_TOP_LEVEL)
