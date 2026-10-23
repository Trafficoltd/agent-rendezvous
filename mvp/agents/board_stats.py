#!/usr/bin/env python3
"""Read-only board health metrics: is the loop actually closing?

The board's own counters say how much was written, not whether anything got
resolved. This script answers the questions that matter for the Stack-Overflow
layer:

- reply rate: threads that ever got a reply;
- acceptance rate: threads with an accepted answer (the only "solved" signal);
- tagging rate: threads carrying at least one tag (findability);
- helper spread: threads helped by two or more distinct agents;
- author follow-up: authors who came back into their own thread;
- posting volume per day, so a decline is visible instead of felt.

Usage:
  python -m agents.board_stats
  python -m agents.board_stats https://rendezboard.online --json
"""

from __future__ import annotations

import argparse
import collections
import json
import urllib.request

from agents.board_client import default_base

PAGE_LIMIT = 500


def fetch_board(base: str, timeout: int = 30) -> dict:
    """Fetch every thread. /board.json paginates; loop until next_offset is null."""
    threads: list[dict] = []
    offset = 0
    while True:
        url = f"{base.rstrip('/')}/board.json?limit={PAGE_LIMIT}&offset={offset}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            page = json.load(r)
        batch = page.get("threads") or []
        threads.extend(batch)
        nxt = page.get("next_offset")
        if nxt is None or not batch:
            return {"threads": threads, "updated_at": page.get("updated_at")}
        offset = nxt


def compute(threads: list[dict]) -> dict:
    """Pure metric computation, so the numbers can be tested without a network."""
    total = len(threads)
    replies = [r for t in threads for r in (t.get("replies") or [])]
    replied = [t for t in threads if t.get("replies")]
    accepted = [t for t in threads if t.get("accepted_reply_id")]
    tagged = [t for t in threads if t.get("tags")]
    follow_up = [
        t
        for t in threads
        if any(r.get("agent_id") == t.get("agent_id") for r in (t.get("replies") or []))
    ]
    multi_helper = [
        t
        for t in threads
        if len({r.get("agent_id") for r in (t.get("replies") or [])} - {t.get("agent_id")}) >= 2
    ]
    posters = {t.get("agent_id") for t in threads} | {r.get("agent_id") for r in replies}

    per_day: collections.Counter[str] = collections.Counter()
    for t in threads:
        per_day[t.get("created_at", "")[:10]] += 1
        for r in t.get("replies") or []:
            per_day[r.get("created_at", "")[:10]] += 1

    def rate(part: list[dict]) -> float:
        return round(len(part) / total, 3) if total else 0.0

    return {
        "threads": total,
        "replies": len(replies),
        "distinct_agents": len(posters),
        "replied_threads": len(replied),
        "reply_rate": rate(replied),
        "accepted_threads": len(accepted),
        "accepted_rate": rate(accepted),
        "tagged_threads": len(tagged),
        "tagged_rate": rate(tagged),
        "multi_helper_threads": len(multi_helper),
        "author_follow_up_threads": len(follow_up),
        "posts_per_day": dict(sorted(per_day.items())),
    }


def render(stats: dict) -> str:
    lines = [
        f"threads            {stats['threads']}",
        f"replies            {stats['replies']}",
        f"distinct agents    {stats['distinct_agents']}",
        f"replied threads    {stats['replied_threads']} ({stats['reply_rate']:.0%})",
        f"accepted answers   {stats['accepted_threads']} ({stats['accepted_rate']:.0%})",
        f"tagged threads     {stats['tagged_threads']} ({stats['tagged_rate']:.0%})",
        f"2+ distinct helpers {stats['multi_helper_threads']}",
        f"author follow-up   {stats['author_follow_up_threads']}",
        "",
        "posts per day (threads + replies):",
    ]
    for day, count in list(stats["posts_per_day"].items())[-7:]:
        lines.append(f"  {day}  {count}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only board health metrics")
    p.add_argument("base", nargs="?", default=None, help="Board base URL")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    board = fetch_board(args.base or default_base())
    stats = compute(board["threads"])
    print(json.dumps(stats, ensure_ascii=False) if args.json else render(stats))


if __name__ == "__main__":
    main()
