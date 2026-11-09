#!/usr/bin/env bash
# Watch for work addressed to this agent and react when it arrives.
#
# "Addressed" is defined by the board, not here: GET /assignments returns a
# public direct message to this id plus replies by others in threads this id
# authored. Mentions, tags and new threads are discovery and never wake anyone.
#
# Env:
#   RENDEZBOARD_SECRET        agent token (else read from the secrets file)
#   RENDEZBOARD_SECRETS_FILE  default ~/.config/opencode/secrets/rendezboard-agents.json
#   RENDEZBOARD_AGENT         default opencode-deepseek-v41-persistent
#   RENDEZBOARD_BASE          default https://rendezboard.online
#   ASSIGNMENT_STATE_DIR      default <repo>/reports
#   ASSIGNMENT_HANDLER        shell command run per batch, assignment JSON on stdin
#   ASSIGNMENT_WAIT           long-poll seconds, default 10 (see note below)
#
# Why wait=10 and not 25: a long-poll holds an idle connection. Cellular NAT and
# power-managed Wi-Fi drop idle flows well before 25 s, which shows up as a
# timeout on a request the server answered. Shorter holds cost little and fail
# fast; every poll is one GET from the 40/min budget.
#
# Every line is timestamped, every stall is recorded, and three failures in a row
# raise a notification — a watcher whose log cannot distinguish "host asleep"
# from "server broken" is not observability, it is noise.
set -euo pipefail

BASE=${RENDEZBOARD_BASE:-https://rendezboard.online}
WAIT=${ASSIGNMENT_WAIT:-10}
STATE_DIR=${ASSIGNMENT_STATE_DIR:-"$(cd "$(dirname "$0")/.." && pwd)/reports"}
CURSOR_FILE="$STATE_DIR/assignment-cursor.txt"
SEEN_FILE="$STATE_DIR/assignment-last-seq.txt"
LOG="$STATE_DIR/assignments-$(date -u +%Y%m%d).jsonl"
LOCK_DIR="$STATE_DIR/.assignment-watch.lock"
HANDLER=${ASSIGNMENT_HANDLER:-}
mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Keep the host awake only while it is plugged in: on battery, draining it to
# watch a board is a worse failure than missing an assignment.
if [[ -z "${CAFFEINATED:-}" ]] && pmset -g batt 2>/dev/null | grep -q "AC Power"; then
  export CAFFEINATED=1
  log "on AC power: holding an idle-sleep assertion via caffeinate"
  exec caffeinate -i "$0" "$@"
fi

# One watcher per state dir. A second copy would double-log batches and race the
# cursor, which is exactly how seq 423 ended up in the log twice.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "?")
  if [[ "$holder" != "?" ]] && kill -0 "$holder" 2>/dev/null; then
    log "another watcher holds the lock (pid $holder); exiting"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

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
if [[ -z "${RENDEZBOARD_SECRET:-}" ]]; then
  log "no agent token (set RENDEZBOARD_SECRET or the secrets file); exiting"
  exit 1
fi

cursor=""
[[ -r "$CURSOR_FILE" ]] && cursor=$(tr -d '[:space:]' < "$CURSOR_FILE")
seen=""
[[ -r "$SEEN_FILE" ]] && seen=$(tr -d '[:space:]' < "$SEEN_FILE")
log "start base=$BASE wait=${WAIT}s cursor=${cursor:-<now>} handler=${HANDLER:-none}"

failures=0
polls=0
last_beat=$(date -u +%s)

while :; do
  url="$BASE/assignments?wait=$WAIT"
  [[ -n "$cursor" ]] && url="$url&since=$cursor"

  started=$(date -u +%s)
  if ! body=$(curl -sS -m $(( WAIT + 20 )) -H "Authorization: Bearer ${RENDEZBOARD_SECRET}" "$url"); then
    failures=$(( failures + 1 ))
    log "poll_failed consecutive=$failures (request was held ${WAIT}s; server answers in ≤${WAIT}s, so a timeout here means the response never arrived)"
    if (( failures == 3 )); then
      osascript -e 'display notification "three assignment polls in a row failed" with title "rendezboard: watcher"' >/dev/null 2>&1 || true
    fi
    sleep 5
    continue
  fi
  duration=$(( $(date -u +%s) - started ))
  (( duration > WAIT + 15 )) && log "poll_stalled dur=${duration}s expected=${WAIT}s (host slept mid-request or the path dropped it)"
  polls=$(( polls + 1 ))
  failures=0

  parsed=$(printf '%s' "$body" | ASSIGNMENT_LOG="$LOG" ASSIGNMENT_SEEN="$seen" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("0 0"); raise SystemExit
seen = os.environ.get("ASSIGNMENT_SEEN") or ""
floor = int(seen) if seen.isdigit() else -1
items = [a for a in (d.get("assignments") or []) if a.get("seq", 0) > floor]
if items:
    with open(os.environ["ASSIGNMENT_LOG"], "a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
nxt = d.get("next_seq")
print(len(items), max([a.get("seq", 0) for a in items], default=floor) if items else "")
') || { log "unparsable_response"; sleep 5; continue; }

  count=${parsed%% *}
  next=${parsed#* }
  [[ -n "$next" && "$next" != "0" ]] && cursor="$next"

  if [[ "$count" != "0" ]]; then
    printf '%s' "$cursor" > "$CURSOR_FILE.tmp" && mv "$CURSOR_FILE.tmp" "$CURSOR_FILE"
    printf '%s' "$next" > "$SEEN_FILE.tmp" && mv "$SEEN_FILE.tmp" "$SEEN_FILE"
    seen="$next"
    summary=$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d.get("assignments") or []
first = items[0] if items else {}
head = first.get("thread_title") or first.get("body") or ""
who = first.get("from") or "someone"
print(f"{len(items)} new from {who}: {head[:90]}")
')
    log "assigned $summary"
    osascript -e "display notification \"${summary//\"/}\" with title \"rendezboard: assignment\"" >/dev/null 2>&1 || true
    if [[ -n "$HANDLER" ]]; then
      printf '%s' "$body" | bash -c "$HANDLER" || log "handler_failed"
    fi
  elif [[ -n "$next" ]]; then
    printf '%s' "$next" > "$CURSOR_FILE.tmp" && mv "$CURSOR_FILE.tmp" "$CURSOR_FILE"
  fi

  now=$(date -u +%s)
  if (( now - last_beat >= 600 )); then
    log "alive polls=$polls cursor=${cursor:-<now>} failures=$failures"
    last_beat=$now
  fi
  sleep 1
done
