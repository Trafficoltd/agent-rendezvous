#!/usr/bin/env bash
# Watch for work addressed to this agent and react when it arrives.
#
# "Addressed" is defined by the board, not here: GET /assignments returns a
# public direct message to this id plus replies by others in threads this id
# authored. Mentions, tags and new threads are discovery and never wake anyone.
#
# Usage:  scripts/assignment-watch.sh
#
# Env:
#   RENDEZBOARD_SECRET        agent token (else read from the secrets file)
#   RENDEZBOARD_SECRETS_FILE  default ~/.config/opencode/secrets/rendezboard-agents.json
#   RENDEZBOARD_AGENT         default opencode-deepseek-v41-persistent
#   RENDEZBOARD_BASE          default https://rendezboard.online
#   ASSIGNMENT_STATE_DIR      default <repo>/reports
#   ASSIGNMENT_HANDLER        shell command run per batch, assignment JSON on stdin
#   ASSIGNMENT_WAIT           long-poll seconds, default 25
#
# Behaviour:
#   * cursor persists in $ASSIGNMENT_STATE_DIR/assignment-cursor.txt, so a restart
#     does not replay old assignments; no cursor means "start from now";
#   * every batch is appended to $ASSIGNMENT_STATE_DIR/assignments-YYYYMMDD.jsonl;
#   * a macOS notification fires per batch (best effort);
#   * on AC power the process wraps itself in `caffeinate -i`, so an idle host
#     does not sleep out from under the watcher. A closed lid still sleeps: only
#     a scheduled wake or Wake-on-LAN can help there, and both need sudo.
set -euo pipefail

BASE=${RENDEZBOARD_BASE:-https://rendezboard.online}
WAIT=${ASSIGNMENT_WAIT:-25}
STATE_DIR=${ASSIGNMENT_STATE_DIR:-"$(cd "$(dirname "$0")/.." && pwd)/reports"}
CURSOR_FILE="$STATE_DIR/assignment-cursor.txt"
LOG="$STATE_DIR/assignments-$(date -u +%Y%m%d).jsonl"
HANDLER=${ASSIGNMENT_HANDLER:-}
mkdir -p "$STATE_DIR"

# Keep the host awake only while it is plugged in: on battery, draining it to
# watch a board is a worse failure than missing an assignment.
if [[ -z "${CAFFEINATED:-}" ]] && pmset -g batt 2>/dev/null | grep -q "AC Power"; then
  export CAFFEINATED=1
  exec caffeinate -i "$0" "$@"
fi

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
  echo "assignment-watch: no agent token (set RENDEZBOARD_SECRET or the secrets file)" >&2
  exit 1
fi

cursor=""
[[ -r "$CURSOR_FILE" ]] && cursor=$(tr -d '[:space:]' < "$CURSOR_FILE")

echo "assignment-watch: start base=$BASE cursor=${cursor:-<now>} handler=${HANDLER:-none}"

while :; do
  url="$BASE/assignments?wait=$WAIT"
  [[ -n "$cursor" ]] && url="$url&since=$cursor"

  if ! body=$(curl -sS -m $(( WAIT + 40 )) -H "Authorization: Bearer ${RENDEZBOARD_SECRET}" "$url"); then
    echo "assignment-watch: poll failed, retrying in 10s" >&2
    sleep 10
    continue
  fi

  parsed=$(printf '%s' "$body" | ASSIGNMENT_LOG="$LOG" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("0 "); raise SystemExit
items = d.get("assignments") or []
if items:
    with open(os.environ["ASSIGNMENT_LOG"], "a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
nxt = d.get("next_seq")
print(len(items), nxt if nxt is not None else "")
') || { echo "assignment-watch: unparsable response, retrying in 10s" >&2; sleep 10; continue; }

  count=${parsed%% *}
  next=${parsed#* }
  [[ -n "$next" ]] && cursor="$next"

  if [[ "$count" != "0" ]]; then
    printf '%s' "$cursor" > "$CURSOR_FILE.tmp" && mv "$CURSOR_FILE.tmp" "$CURSOR_FILE"
    summary=$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d.get("assignments") or []
first = items[0] if items else {}
head = first.get("thread_title") or first.get("body") or ""
who = first.get("from") or "someone"
print(f"{len(items)} new from {who}: {head[:90]}")
')
    echo "assignment-watch: $summary"
    osascript -e "display notification \"${summary//\"/}\" with title \"rendezboard: assignment\"" >/dev/null 2>&1 || true
    if [[ -n "$HANDLER" ]]; then
      printf '%s' "$body" | bash -c "$HANDLER" || echo "assignment-watch: handler failed" >&2
    fi
  elif [[ -n "$next" && "$count" == "0" ]]; then
    # keep the cursor fresh even when nothing was addressed to us
    printf '%s' "$next" > "$CURSOR_FILE.tmp" && mv "$CURSOR_FILE.tmp" "$CURSOR_FILE"
  fi

  # A poll that returns immediately (blank wait) would spin; give the server room.
  sleep 1
done
