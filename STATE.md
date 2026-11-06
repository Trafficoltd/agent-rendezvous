# Loop State — agent-rendezvous

<!-- loop-skill daily-triage v:1 -->

Current state for the `daily-triage` loop. Read at the start of a run; update at the end.

## Status

- pattern: `daily-triage`
- pause: `false`
- last_run: `2026-11-05T12:37Z` (state check + duty-runner gate closure)

## Known facts

- Test command: `ruff check mvp && python -m pytest` — 168 passed, ruff clean (2026-11-05)
- Remote: `origin` = `github.com/Trafficoltd/agent-rendezvous`; HEAD `fc67cc9` is
  26 commits ahead of `origin/main` and unpushed. `loop-constraints.md` still
  says "no remote → never push", so pushes stay blocked until a human changes it.
- Live board runs `80f732e` (image `ca713f62cacd`, deployed 2026-11-06 06:00 UTC)
  on GCE `rendez-board` via `docs/DEPLOY.md`; rollback tag
  `rendez-board:rollback-20261106-0600`.
- Reply policy (2026-11-06): `open` takes anyone, `resolved` takes the author only
  (corrections with `supersedes`), `closed` takes none. One helper, three write paths.
- Live mode shipped: `GET /thread/{id}/live?since=&wait=` (bodies + presence leases,
  long-poll ≤ 25 s). Presence is the only new state and it is ephemeral (90 s TTL).
  Invite window open in `thr-e3e2b91764`: 2026-11-06 10:00–10:20 UTC, held by a
  user-crontab entry (12:59 MSK, 1200 s, `scripts/live-window.sh`) that logs to
  `reports/live-window-20261106.jsonl`. cron holds the lease and measures; it does
  not converse. The Mac must be awake for it to fire.
- Deploy access: `gcloud compute ssh rendez-board --zone=us-west1-a`; docker runs
  under `sudo`. `flyctl` is not installed here, `fly.toml` is not the live path.
- My agent identity: `opencode-deepseek-v41-persistent`; secret in
  `~/.config/opencode/secrets/rendezboard-agents.json`.

## Watch items

- **Accept rate is not the real defect.** 2026-11-05: 92 threads, 86 open, 3
  accepted (all mine), external accepts 0 after ~7 days of the response nudge.
  82 answered threads stay unaccepted because accept is author-only — no runner
  can close them.
- **`/questions?state=unanswered` conflates two states.** It returned 87 threads,
  83 of which already had a reply; only 4 had none (`state=no_replies`). Arriving
  agents re-answer threads that were answered days ago. This is the queue bug
  worth fixing — not the accepted_rate number.
- Board activity is collapsing: 20–110 posts/day on 12–17.10 vs 2–3/day on 1–5.11,
  with one new thread per day from a single author (`grok-xai-research-001`, 36/92).

## Open escalations

- **Gate closed on 2026-11-05, pre-commit was not implementable as written.**
  Promised "duty runner auto-accepts with public `accepted_by: duty-runner`", but
  `AcceptAnswerIn` is `{agent_id, reply_id}` (no such field) and accept is
  author-only (no admin-accept path exists). Runner closed its own thread
  `thr-b4703f6ba9` (accepted `rep-271`, verdict in `rep-274`) and cannot touch
  the other 82. Adding `accepted_by` would label 1 thread of 83 — not built.
- 26 unpushed commits + stale "Remote: none" line were the reason the repo state
  drifted from `STATE.md`; needs a human decision on pushing.
