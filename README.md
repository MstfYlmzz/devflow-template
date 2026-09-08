# devflow-template

Template repository for the `devflow` CLI and a project skeleton to copy into new repos.

## Verify

Install the package with dev dependencies, then run the verification pipeline:

```bash
pip install -e ".[dev]"
./scripts/verify
```

After cloning, run `./scripts/setup-hooks`.

To mimic CI — no host git identity — run:

```bash
HOME=$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null ./scripts/verify
```
