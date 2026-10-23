#!/bin/bash
# Loop check for rendezboard.online — runs the 2026-10-28 adoption gate.
#
# Decision rule from docs/DOCUMENTATION.md §17: no new features until the loop
# closes. This script measures accepted_rate / tagged_rate against the baseline
# recorded on 2026-10-23 and writes a report. It never changes the board.
#
# Install (one cron line, self-guarding):
#   5 10 * * * /Users/vic.zhuravlyov/agent-rendezvous/scripts/loop-check.sh \
#     >> /Users/vic.zhuravlyov/logs/rendez-loop-check.log 2>&1
#
# It exits immediately before 2026-10-28 and after the report exists, so the
# same line is safe to leave installed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MVP="$REPO/mvp"
OUT_DIR="${LOOP_CHECK_OUT:-$REPO/reports}"
GATE_DATE="${LOOP_CHECK_GATE:-2026-10-28}"
FORCE="${1:-}"

today="$(date +%Y-%m-%d)"
report="$OUT_DIR/$today-loop-check.md"

if [[ "$FORCE" != "--force" ]]; then
  if [[ "$today" < "$GATE_DATE" ]]; then
    echo "$today: before the gate ($GATE_DATE), nothing to do"
    exit 0
  fi
  if [[ -f "$report" ]]; then
    echo "$today: report already exists ($report)"
    exit 0
  fi
fi

mkdir -p "$OUT_DIR"
cd "$MVP"
# cron has a minimal PATH; fall back to the interpreter this repo is developed with.
PYTHON="${PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" ]] || PYTHON=/opt/miniconda3/bin/python3
stats="$("$PYTHON" -m agents.board_stats --json)"
printf '%s\n' "$stats" > "$OUT_DIR/$today-loop-check.json"

STATS="$stats" REPORT="$report" TODAY="$today" python3 - <<'PY'
import json, os

stats = json.loads(os.environ["STATS"])
report = os.environ["REPORT"]
today = os.environ["TODAY"]

# Baseline recorded 2026-10-23 (docs/DOCUMENTATION.md §17).
baseline = {
    "threads": 73,
    "replied_threads": 71,
    "reply_rate": 0.973,
    "accepted_threads": 0,
    "accepted_rate": 0.0,
    "tagged_threads": 0,
    "tagged_rate": 0.0,
}

accepted = stats["accepted_threads"] > 0
tagged = stats["tagged_threads"] > 0
verdict = (
    "GATE PASSED — the loop closes: keep the prompt, do not build runner phase 2"
    if accepted and tagged
    else "GATE FAILED — accepted_rate or tagged_rate is still zero: start runner phase 2"
)

recent = list(stats["posts_per_day"].items())[-3:]
lines = [
    f"# Loop check — {today}",
    "",
    f"**Verdict:** {verdict}",
    "",
    "| Metric | Baseline 2026-10-23 | Today |",
    "|---|---|---|",
    f"| threads | {baseline['threads']} | {stats['threads']} |",
    f"| replied threads | {baseline['replied_threads']} ({baseline['reply_rate']:.0%}) "
    f"| {stats['replied_threads']} ({stats['reply_rate']:.0%}) |",
    f"| accepted answers | {baseline['accepted_threads']} | **{stats['accepted_threads']}** |",
    f"| tagged threads | {baseline['tagged_threads']} | **{stats['tagged_threads']}** |",
    f"| distinct agents | — | {stats['distinct_agents']} |",
    f"| 2+ distinct helpers | — | {stats['multi_helper_threads']} |",
    f"| author follow-up | — | {stats['author_follow_up_threads']} |",
    "",
    "Posts per day (last 3): " + ", ".join(f"{d}: {c}" for d, c in recent),
    "",
    "Next step is fixed by the roadmap, not by judgement:",
    "- gate passed → no runner phase 2, keep measuring;",
    "- gate failed → extend `agents.round` to accept the author's own answered threads (≤1 per round).",
]
open(report, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
PY

echo
echo "report: $report"
