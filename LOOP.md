# Loop Configuration — agent-rendezvous

> Scaffolded by `loop-init` on 2026-10-01. Edit as needed; keep the section names — the loop skills read this file.

## Active Loops

| Pattern | Cadence | Status | Command |
|---------|---------|--------|---------|
| `daily-triage` | 1d | L1 report-only | `opencode run "Run loop-triage"` (via cron/systemd) |

## Human Gates

- No auto-fix until the L2 checklist is complete.
- High-risk paths require human review (see the `Paths` section of `loop-constraints.md`).
- Skill/guideline changes go through `loop-improve-skill` as a proposal — never self-applied.

## Worktrees

- Use an explicit `git worktree` and run opencode with `--dir <worktree>` for implementer runs (L2+).
- One worktree per fix attempt; discard after the verifier rejects.

## Budget

- Max sub-agent spawns per run: 0 (L1) / 2 (L2).
- If token spend hits 80% of the daily cap, switch to report-only.
- If `loop-pause-all` is active, exit immediately.

## Links

- Constraints: `loop-constraints.md`
- Budget: `loop-budget.md`
- State: `STATE.md`
- Run log: `loop-run-log.md`
