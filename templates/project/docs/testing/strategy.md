## What verify runs

<!-- TODO: list verify stages -->

To mimic CI (no host git identity):

```bash
HOME=$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null ./scripts/verify
```

CI type-checks on Linux. Platform-specific APIs (for example `ctypes.windll`)
are invisible to that mypy run even when they type-check on Windows.

## Test levels

<!-- TODO: describe test levels -->

## What is not tested

<!-- TODO: describe what is not tested -->
