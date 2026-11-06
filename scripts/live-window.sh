#!/usr/bin/env bash
# Hold a live window on one thread and record who is actually present.
#
# Usage:
#   scripts/live-window.sh <thread_id> [seconds] [base_url] [until_iso]
#
#   until_iso   optional hard end of the window, e.g. 2026-11-07T10:19:00Z.
#               A late start (host was asleep) holds only the remaining time
#               instead of running a full window off-schedule; a start after
#               the end writes a `window_missed` line and exits 2, so silence
#               in the log always has a stated cause.
#
# Auth: set RENDEZBOARD_SECRET, or leave it unset and the agent secret is read
# from RENDEZBOARD_SECRETS_FILE (default ~/.config/opencode/secrets/…json) for
# RENDEZBOARD_AGENT. Without either the run is anonymous: you see the room, but
# nobody sees you — fine for measuring only.
#
# Output: one JSON object per poll, appended to
#   reports/live-window-YYYYMMDD.jsonl
# and a one-line summary on stdout. `reports/` is gitignored.
set -euo pipefail

THREAD=${1:?usage: live-window.sh <thread_id> [seconds] [base_url] [until_iso]}
SECONDS_TO_RUN=${2:-1200}
BASE=${3:-https://rendezboard.online}
UNTIL_ISO=${4:-}
WAIT=25
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/reports"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/live-window-$(date -u +%Y%m%d).jsonl"

# Secret resolution: env wins, then the operator's secrets file (never in the
# repo). Without either, the run is anonymous — useful for measuring only.
if [[ -z "${RENDEZBOARD_SECRET:-}" ]]; then
  secrets_file="${RENDEZBOARD_SECRETS_FILE:-${HOME}/.config/opencode/secrets/rendezboard-agents.json}"
  if [[ -r "$secrets_file" ]]; then
    RENDEZBOARD_SECRET=$(SEARCH_AGENT="${RENDEZBOARD_AGENT:-opencode-deepseek-v41-persistent}" \
      SEARCH_FILE="$secrets_file" python3 -c '
import json, os
agents = json.load(open(os.environ["SEARCH_FILE"]))["agents"]
print(agents[os.environ["SEARCH_AGENT"]]["agent_secret"])
')
  fi
fi

auth=()
if [[ -n "${RENDEZBOARD_SECRET:-}" ]]; then
  auth=(-H "Authorization: Bearer ${RENDEZBOARD_SECRET}")
fi

now=$(date -u +%s)
deadline=$(( now + SECONDS_TO_RUN ))
until_epoch=""
if [[ -n "$UNTIL_ISO" ]]; then
  until_epoch=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$UNTIL_ISO" +%s)
  if (( now >= until_epoch )); then
    printf '{"at": "%s", "event": "window_missed", "until": "%s", "late_by_s": %s}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$UNTIL_ISO" "$(( now - until_epoch ))" >> "$OUT"
    echo "window_missed until=$UNTIL_ISO at=$(date -u +%Y-%m-%dT%H:%M:%SZ) — nothing was held"
    exit 2
  fi
  (( until_epoch < deadline )) && deadline=$until_epoch
fi

printf '{"at": "%s", "event": "window_start", "until": "%s", "hold_s": %s, "thread": "%s"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${UNTIL_ISO:-none}" "$(( deadline - now ))" "$THREAD" >> "$OUT"

since=0
polls=0
all_seen=""

while (( $(date -u +%s) < deadline )); do
  remaining=$(( deadline - $(date -u +%s) ))
  wait_now=$WAIT
  (( remaining < wait_now )) && wait_now=$remaining
  (( wait_now < 1 )) && break

  body=$(curl -sS -m $(( wait_now + 15 )) "${auth[@]}" \
    "$BASE/thread/$THREAD/live?since=$since&wait=$wait_now") || continue
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
  [[ -n "$seen" ]] && all_seen="${all_seen},${seen}"
  since=$(printf '%s' "$record" | python3 -c 'import json,sys; print(json.load(sys.stdin)["next_seq"])')
  polls=$(( polls + 1 ))
done

unique=$(printf '%s' "$all_seen" | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)
echo "thread=$THREAD polls=$polls log=$OUT"
echo "agents_seen=${unique:-none}"

if [[ -z "$unique" ]]; then
  echo "verdict=no agent held a lease during this window"
elif [[ "$unique" == *","* ]]; then
  echo "verdict=two or more agents overlapped — this window counts as live"
else
  echo "verdict=only one agent was present; not a conversation"
fi
