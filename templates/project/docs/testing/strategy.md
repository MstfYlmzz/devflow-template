## What verify runs

<!-- TODO: list verify stages -->

To mimic CI (no host git identity):

```bash
HOME=$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null ./scripts/verify
```

`scripts/verify.d/30-typecheck` runs `mypy --platform linux` so local typing
matches CI. Platform-only APIs such as `ctypes.windll` are invisible to that
run even when the host is Windows.

## Test levels

<!-- TODO: describe test levels -->

## What is not tested

<!-- TODO: describe what is not tested -->
