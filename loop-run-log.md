# Loop Run Log — agent-rendezvous

> Append-only log of loop runs. One JSON object per run (see `loop-budget`).
> Decisions a run produces carry `<!-- loop-skill <name> v:<N> -->` on their first line — never in source code.

## Format

```json
{
  "run_id": "<ISO8601>",
  "pattern": "daily-triage",
  "duration_s": 0,
  "items_found": 0,
  "actions_taken": 0,
  "escalations": 0,
  "tokens_estimate": 0,
  "outcome": "no-op | report-only | fix-proposed | escalated"
}
```

## Runs

<!-- Newest first. Prune entries older than 30 days. -->

_(нет запусков)_
