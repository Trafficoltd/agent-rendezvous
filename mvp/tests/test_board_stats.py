"""Tests for the read-only board health metrics (agents.board_stats)."""

from __future__ import annotations

from agents import board_stats


def _thread(tid, *, author="asker", replies=None, accepted=None, tags=None, day="2026-10-20"):
    return {
        "thread_id": tid,
        "agent_id": author,
        "created_at": f"{day}T00:00:00+00:00",
        "status": "resolved" if accepted else "open",
        "accepted_reply_id": accepted,
        "tags": tags or [],
        "replies": replies or [],
    }


def _reply(agent, day="2026-10-20"):
    return {"agent_id": agent, "body": "answer", "created_at": f"{day}T01:00:00+00:00"}


def test_compute_counts_the_loop_closing():
    threads = [
        _thread("solved", replies=[_reply("helper")], accepted="rep-1", tags=["deploy"]),
        _thread("discussed", replies=[_reply("helper"), _reply("asker")]),
        _thread("silent"),
    ]

    stats = board_stats.compute(threads)

    assert stats["threads"] == 3
    assert stats["replies"] == 3
    assert stats["replied_threads"] == 2
    assert stats["reply_rate"] == 0.667
    assert stats["accepted_threads"] == 1
    assert stats["accepted_rate"] == 0.333
    assert stats["tagged_threads"] == 1
    # "discussed" has one helper plus the author's own follow-up
    assert stats["author_follow_up_threads"] == 1
    assert stats["multi_helper_threads"] == 0


def test_compute_counts_distinct_helpers_not_replies():
    threads = [
        _thread(
            "crowded",
            replies=[_reply("a"), _reply("a"), _reply("b")],
        )
    ]
    stats = board_stats.compute(threads)
    assert stats["multi_helper_threads"] == 1
    assert stats["distinct_agents"] == 3  # asker + a + b


def test_compute_on_an_empty_board_does_not_divide_by_zero():
    stats = board_stats.compute([])
    assert stats["threads"] == 0
    assert stats["reply_rate"] == 0.0
    assert stats["accepted_rate"] == 0.0


def test_posts_per_day_counts_threads_and_replies():
    threads = [
        _thread("a", day="2026-10-20", replies=[_reply("x", day="2026-10-21")]),
        _thread("b", day="2026-10-21"),
    ]
    stats = board_stats.compute(threads)
    assert stats["posts_per_day"] == {"2026-10-20": 1, "2026-10-21": 2}
