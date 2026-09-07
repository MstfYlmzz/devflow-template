# Review findings

Each finding uses these fields: `id`, `severity`, `category`, `location`,
`problem`, `failure_scenario`, `expected_behavior`, `evidence`, `blocking`.

## Merge

| Severity | Merge |
| --- | --- |
| BLOCKER | forbidden |
| HIGH | forbidden |
| MEDIUM | fix or waive |
| LOW | non-blocking |

## Example

```yaml
id: F1
severity: HIGH
category: correctness
location: src/payments/charge.py:41
problem: Charge succeeds after the ledger write fails.
failure_scenario: Process dies between payment capture and ledger insert.
expected_behavior: Capture and ledger write commit together or neither does.
evidence: test
blocking: true
```
