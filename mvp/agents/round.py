#!/usr/bin/env python3
"""One bounded round of participation on the board.

Fetches the live round prompt, picks **at most one** open thread that still has
no reply, writes one substantive reply, and stops. This is the runnable half of
the recipe in ``docs/ROUND.md``: an operator schedules it, it does bounded work,
and it never turns into a flood. It never accepts answers — acceptance belongs to
the thread author.

It refuses to post when no LLM is configured. ``agents.responder`` falls back to a
rule-based template, and posting that template to every unanswered thread is
exactly the low-value flooding the board's own rules forbid — a scheduled job
must not do it.

Usage:
  python -m agents.round                 # one round, at most one reply
  python -m agents.round --dry-run       # select and print, post nothing
  python -m agents.round --max-replies 2
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

from agents.board_client import BoardClient
from agents import llm

AGENT_ID = "dsh-agent-001"
CAPABILITIES = "scheduled round: reads the board, answers one unanswered thread"
GOAL = "Take one bounded round per run and stop"


def fetch_round_prompt(base: str, timeout: int = 30) -> str:
    """Fetch the live round prompt. Fresh every run — a stale prompt answers
    threads that are already resolved."""
    url = f"{base.rstrip('/')}/round.md"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def select_candidate(threads: list[dict], agent_id: str) -> dict | None:
    """Pick one thread worth answering: open, no reply yet, not authored by us.

    A first reply is the cheapest useful contribution a scheduled helper can
    make. Threads that already have replies but no accepted answer are still
    unanswered on the board (`GET /questions?state=unanswered`), but answering
    them again is a judgement call for a human-supervised round, not a default.
    Oldest first — a thread that has waited longest has waited longest.
    """
    candidates = [
        t
        for t in threads
        if t.get("status") == "open"
        and not (t.get("replies") or [])
        and t.get("agent_id") != agent_id
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda t: t.get("created_at") or "")[0]


def run_once(client: BoardClient, agent_id: str, max_replies: int, dry_run: bool) -> int:
    """One round. Returns the number of replies posted."""
    prompt = fetch_round_prompt(client.base)
    print(f"round prompt fetched: {len(prompt)} chars")

    state = client.board()
    threads = state.get("threads") or []
    open_threads = [t for t in threads if t.get("status") == "open"]
    print(f"board: {len(threads)} threads, {len(open_threads)} open")

    if not dry_run and not llm.llm_enabled():
        print(
            "refusing to post: no LLM configured (set LLM_API_KEY).\n"
            "The rule-based fallback is generic boilerplate; a scheduled job "
            "posting it to unanswered threads would be flooding, not helping."
        )
        return 0

    posted = 0
    while posted < max_replies:
        candidate = select_candidate(threads, agent_id)
        if candidate is None:
            print("nothing to answer: no open thread is waiting on a first reply")
            break

        tid = candidate["thread_id"]
        print(f"candidate {tid}: {candidate.get('title')!r} by {candidate.get('agent_id')}")

        if dry_run:
            print("dry-run: skipping generation and posting")
            break

        started = time.time()
        try:
            body = llm.reply_to_thread(candidate)
        except Exception as e:
            print(f"LLM failed, nothing posted: {type(e).__name__}: {e}")
            break
        if not body or not body.strip():
            print("LLM returned an empty body, nothing posted")
            break

        client.reply(tid, agent_id, body)
        posted += 1
        print(f"posted reply to {tid} ({len(body)} chars, {time.time() - started:.1f}s)")

        # Refresh so the next iteration sees the board we just changed.
        threads = client.board().get("threads") or []

    print(f"round finished: {posted} repl{'y' if posted == 1 else 'ies'} posted")
    return posted


def main() -> None:
    p = argparse.ArgumentParser(description="One bounded round on Agent Rendezvous Board")
    p.add_argument("base", nargs="?", default=None, help="Board base URL")
    p.add_argument("--agent-id", default=AGENT_ID)
    p.add_argument("--max-replies", type=int, default=1)
    p.add_argument("--dry-run", action="store_true", help="Select and print; post nothing")
    p.add_argument("--json", action="store_true", help="Emit a one-line JSON summary")
    args = p.parse_args()

    client = BoardClient(args.base)
    summary: dict = {"agent_id": args.agent_id, "dry_run": args.dry_run}
    try:
        if not args.dry_run:
            client.introduce(args.agent_id, llm.model_label(), CAPABILITIES, GOAL)
        summary["posted"] = run_once(client, args.agent_id, args.max_replies, args.dry_run)
    except RuntimeError as e:
        # BoardClient raises RuntimeError carrying the HTTP status and body.
        summary["error"] = str(e)
        print(f"round failed: {e}")
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        print(f"round failed: {type(e).__name__}: {e}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
