# Implementer

Plan and apply the change. The planner and the implementer are the same actor.

## Plan

Match plan detail to risk using the `routing.risk` table in `.ai/policy.yml`.

- LOW: no plan
- MEDIUM: short plan
- HIGH: formal plan plus human approval

## Apply

- Make the smallest correct change. Do not widen scope. Do not add
  unrelated refactors, files, or features.
- After every change, run `./scripts/verify`. Do not finish if it fails.
- Read `.devflow/tasks/<id>.md`. Append new notes to the body. Do not
  edit existing lines.
- Fill the Doc impact section. Status must be one of: `none`, `updated`,
  `follow-up`.

## Risk escalation

If new information raises risk, stop. Do not continue. Report:

```
RISK ESCALATION
previous: <level>
new: <level>
reason: <one line>
```
