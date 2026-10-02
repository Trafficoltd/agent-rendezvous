# Loop Constraints — agent-rendezvous

> Binding rules for any loop run in this repo. The `loop-constraints` skill reads this file at the
> start of every run and enforces it before every action. If a rule blocks an action — stop and tell the human.

Loop pattern: `daily-triage`.

## Push & Merge

- No remote — write proposals to a file and escalate; never push.
- Never auto-merge to main without human approval.
- Never close an issue or PR without human approval.

## Paths

- Never edit `.env`, `.env.*`, `auth/`, `payments/`, `secrets/`, `credentials/`.
- Never edit infrastructure configs (`fly.toml`, `render.yaml`, `docker-compose.yml`, `Dockerfile`) without approval.
- Never edit generated/build artifacts by hand — regenerate them.

## Code

- Always run the project tests before proposing a fix: `ruff check mvp && python -m pytest`.
- Never disable tests to make CI green.
- Never refactor unrelated code — one fix per run.
- Max 3 fix attempts per item; escalate after.

## Communication

- Always tell the human what you are about to do before doing it.

## Budget

- If token spend hits 80% of the daily cap, switch to report-only.
- If `loop-pause-all` is active, exit immediately.

## Skill evolution

- Never self-apply changes to a skill or its guidelines — propose via `loop-improve-skill` (PR or file) and escalate.

## Marker convention

- Decisions recorded in `loop-run-log.md` carry `<!-- loop-skill <name> v:<N> -->` on their first line.
- Markers must NEVER appear in source code or user-facing docs — only in loop records.

---
<!-- Add your own rules below. Use plain English. The loop reads this verbatim. -->
