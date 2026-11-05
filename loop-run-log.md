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

<!-- loop-skill daily-triage v:1 -->
```json
{
  "run_id": "2026-11-05T12:37:00Z",
  "pattern": "daily-triage",
  "duration_s": 540,
  "items_found": 5,
  "actions_taken": 2,
  "escalations": 2,
  "tokens_estimate": 42000,
  "outcome": "escalated"
}
```
- findings: board healthy (17/17 routes 200, 0.7–3.3 s); 92 threads / 86 open /
  3 accepted / 0 external accepts; 83 of 87 "unanswered" threads already had a
  reply; activity down to 2–3 posts/day; 26 commits ahead of `origin/main` and
  unpushed while `STATE.md` claimed "Remote: none"; ruff clean, 168 tests pass.
- actions: posted `rep-274` (gate verdict) and accepted `rep-271` in
  `thr-b4703f6ba9` — the one thread the runner is authorised to close.
- escalations: (1) promised `accepted_by: duty-runner` label has no API field and
  accept is author-only, so the pre-commit cannot cover the 82 third-party
  threads; (2) human decision needed on pushing the 26 commits.
