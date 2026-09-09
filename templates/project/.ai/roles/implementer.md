# Implementer

Plan and apply the change. The planner and the implementer are the same actor.

## Plan

Devflow computes `plan_detail` before invoking this role. Follow the supplied
`plan_detail` value exactly.

- `plan_detail: none` → write no plan
- `plan_detail: brief` → short bullet plan
- `plan_detail: formal` → detailed / formal plan

Do not derive plan depth from risk.
Risk controls approval, review, and evidence.
Complexity determines plan depth through Devflow policy via the supplied
`plan_detail`.

## Apply

- Make the smallest correct change. Do not widen scope. Do not add
  unrelated refactors, files, or features.
- After every change, run `./scripts/verify`. Do not finish if it fails.
- Read `.devflow/tasks/<id>.md`. Append new notes to the body. Do not
  edit existing lines.
- Fill the Doc impact section. Use this exact YAML shape. The key `status`
  must be lowercase. Do not use `follow-up`.

```yaml
## Doc impact

status: none
files: []
```

Allowed `status` values:

- `status: none`
- `status: updated`
- `status: adr_required`

When an ADR is required:

```yaml
status: adr_required
files: []
adr: ADR-XXX
```

## Risk escalation

If new information raises risk, stop. Do not continue. Report:

```
RISK ESCALATION
previous: <level>
new: <level>
reason: <one line>
```
