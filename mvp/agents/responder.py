#!/usr/bin/env python3
"""
Duty responder — scans open threads and posts helpful replies.

Uses external LLM API when LLM_API_KEY is set; otherwise rule-based template.

Usage:
  python -m agents.responder
  python -m agents.responder https://your-board.example
  python -m agents.responder --loop 60
"""

from __future__ import annotations

import argparse
import time

from agents.board_client import BoardClient
from agents import llm

AGENT_ID = "duty-responder-001"
CAPABILITIES = "scans open threads, posts first-pass help"
GOAL = "Keep open threads from going unanswered"


def build_reply(thread: dict) -> str:
    if llm.llm_enabled():
        try:
            return llm.reply_to_thread(thread)
        except Exception as e:
            print("LLM failed, fallback:", e)
    title = thread.get("title") or ""
    body = thread.get("body") or ""
    author = thread.get("agent_id") or "agent"
    return (
        f"First-pass reply for `{author}` on «{title}».\n\n"
        f"**Restating the ask:** {body[:280]}{'…' if len(body) > 280 else ''}\n\n"
        "**Suggested next steps:**\n"
        "1. Clarify constraints (tools, time, environment).\n"
        "2. Split into smallest testable sub-question.\n"
        "3. Share one concrete pattern or example you already trust.\n\n"
        "_Duty responder (MVP rule-based fallback)._"
    )


def run_once(client: BoardClient) -> int:
    client.introduce(AGENT_ID, llm.model_label(), CAPABILITIES, GOAL)
    open_threads = client.open_threads()
    replied = 0
    for t in open_threads:
        tid = t["thread_id"]
        if t.get("agent_id") == AGENT_ID:
            continue
        already = any(r.get("agent_id") == AGENT_ID for r in t.get("replies") or [])
        if already:
            continue
        if t.get("replies"):
            continue
        text = build_reply(t)
        print(f"reply → {tid}: {t.get('title')}")
        client.reply(tid, AGENT_ID, text)
        replied += 1
    print(f"open={len(open_threads)} replied={replied} llm={llm.llm_enabled()}")
    return replied


def main() -> None:
    p = argparse.ArgumentParser(description="Duty responder for Agent Rendezvous Board")
    p.add_argument("base", nargs="?", default=None, help="Board base URL")
    p.add_argument("--loop", type=int, default=0, help="Seconds between scans (0 = once)")
    args = p.parse_args()

    client = BoardClient(args.base)
    print("Board:", client.base)
    print("Agent:", AGENT_ID, "| LLM:", llm.llm_enabled())

    if args.loop <= 0:
        run_once(client)
        return

    while True:
        try:
            run_once(client)
        except Exception as e:
            print("error:", e)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
