# Loop Budget — agent-rendezvous

> Primary loop: **`daily-triage`** (scaffolded by `loop-init` on 2026-10-01).

## Daily limits

| Loop | Max runs/day | Max tokens/day | Max sub-agent spawns/run |
|------|--------------|----------------|--------------------------|
| `daily-triage` | 2 | 100k | 0 (L1) / 2 (L2) |

## On budget exceed

1. Pause schedulers (disable automations / cron).
2. Append an event to `loop-run-log.md`.
3. Notify the human (Slack / issue / `STATE.md` High Priority).

## Kill switch

- Flag: `loop-pause-all`
- Resume only after the human clears the flag in `STATE.md`.

## Alerts This Period

_(none)_
