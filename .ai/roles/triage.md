# Triage

Classify the change. Do not implement. Do not modify files.

## Input

- issue text
- relevant code
- `ARCHITECTURE.md`
- `docs/adr/`

## Output

Emit only a YAML document. Write nothing else.

Allowed values (choose exactly one scalar for each):

- `complexity`: `LOW`, `MEDIUM`, or `HIGH`
- `architecture_impact`: `NONE`, `POSSIBLE`, or `YES`
- each `signals.*` field and `uncertain`: YAML boolean `true` or `false`

The scalar values in the example below are **format examples only**.
Choose values from the allowed sets based on the task.
Do **not** copy placeholder lists or union-style values into the YAML fields.

```yaml
signals:
  transaction_change: false
  concurrency_sensitive: false
  architecture_boundary_change: false
  unfamiliar_area: false
complexity: MEDIUM
architecture_impact: NONE
uncertain: false
reasons:
  - short reason
```

## Rules

- Do not produce a risk level. Policy computes risk.
- If you are unsure, set `uncertain: true`. Do not guess.
- If only missing fields were requested, emit only those fields.
- Prefer quoting `YES` as `"YES"` so YAML does not treat it as a boolean.
