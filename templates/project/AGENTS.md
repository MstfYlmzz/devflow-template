# Map

- `ARCHITECTURE.md` — purpose, stack, modules, external dependencies
- `docs/architecture/` — subsystem notes
- `docs/adr/` — numbered architecture decisions
- `docs/requirements/` — requirements
- `docs/testing/strategy.md` — what verify runs, test levels, and gaps
- `.ai/` — policy, role prompts, and review schema

## Rules

- Read the relevant document before changing a subsystem.
- Prefer the smallest correct change.
- Do not widen the task scope.
- Do not invent a new architectural pattern without approval.
- Every change must pass `./scripts/verify`.

## Recurring review findings

<!-- TODO: add a line here when a third finding in the same category appears -->
