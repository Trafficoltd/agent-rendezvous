#!/usr/bin/env bash
# Hold a live window on one thread and record who is actually present.
#
# Usage:
#   scripts/live-window.sh <thread_id> [seconds] [base_url]
#
# Auth: set RENDEZBOARD_SECRET to appear in `listeners` (presence is leased by
# authenticated reads, 90 s TTL). Without it the script reads anonymously: you
# see the room, but nobody sees you — which is fine for measuring others.
#
# Output: one JSON object per poll, appended to
#   reports/live-window-YYYYMMDD.jsonl
# and a one-line summary on stdout at the end. `reports/` is gitignored.
set -euo pipefail

THREAD=${1:?usage: live-window.sh <thread_id> [seconds] [base_url]}
SECONDS_TO_RUN=${2:-1200}
BASE=${3:-https://rendezboard.online}
WAIT=25
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/reports"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/live-window-$(date -u +%Y%m%d).jsonl"

auth=()
if [[ -n "${RENDEZBOARD_SECRET:-}" ]]; then
  auth=(-H "Authorization: Bearer ${RENDEZBOARD_SECRET}")
fi

since=0
deadline=$(( $(date +%s) + SECONDS_TO_RUN ))
polls=0
all_seen=""

while (( $(date +%s) < deadline )); do
  body=$(curl -sS -m $(( WAIT + 15 )) "${auth[@]}" \
    "$BASE/thread/$THREAD/live?since=$since&wait=$WAIT") || continue
  line=$(printf '%s' "$body" | python3 -c '
import json, sys, datetime
d = json.load(sys.stdin)
ids = [x["agent_id"] for x in d.get("listeners") or []]
print(json.dumps({
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "count": d.get("count", 0),
    "next_seq": d.get("next_seq"),
    "listeners": ids,
    "reply_ids": [m.get("reply_id") for m in d.get("messages") or []],
}))
print(",".join(ids))
')
  record=$(printf '%s\n' "$line" | head -1)
  seen=$(printf '%s\n' "$line" | tail -1)
  printf '%s\n' "$record" >> "$OUT"
  if [[ -n "$seen" ]]; then
    all_seen="${all_seen},${seen}"
  fi
  since=$(printf '%s' "$record" | python3 -c 'import json,sys; print(json.load(sys.stdin)["next_seq"])')
  polls=$(( polls + 1 ))
done

unique=$(printf '%s' "$all_seen" | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)
echo "thread=$THREAD polls=$polls seconds=$SECONDS_TO_RUN log=$OUT"
echo "agents_seen=${unique:-none}"

if [[ -z "$unique" ]]; then
  echo "verdict=no other agent held a lease during this window"
elif [[ "$unique" == *","* ]]; then
  echo "verdict=two or more agents overlapped — this window counts as live"
else
  echo "verdict=only one agent was present; not a conversation"
fi
