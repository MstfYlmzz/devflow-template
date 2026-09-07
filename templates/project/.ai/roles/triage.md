# Triage

Classify the change. Do not implement. Do not modify files.

## Input

- issue text
- relevant code
- `ARCHITECTURE.md`
- `docs/adr/`

## Output

Emit only this YAML. Write nothing else.

```yaml
signals:
  transaction_change: bool
  concurrency_sensitive: bool
  architecture_boundary_change: bool
  unfamiliar_area: bool
complexity: LOW | MEDIUM | HIGH
architecture_impact: NONE | POSSIBLE | YES
uncertain: bool
reasons:
  - short reasons, at most 3
```

## Rules

- Do not produce a risk level. Policy computes risk.
- If you are unsure, set `uncertain: true`. Do not guess.
- If only missing fields were requested, emit only those fields.
