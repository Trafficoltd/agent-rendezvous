"""Tests for the /events cursor feed: triggers, backfill, cursor, long-poll."""

from __future__ import annotations

import sqlite3
import threading
import time


def _thread(client, title: str = "a thread title", agent: str = "owner") -> str:
    return client.post(
        "/thread", json={"agent_id": agent, "title": title, "body": "a body long enough"}
    ).json()["thread_id"]


def test_events_log_thread_reply_and_first_introduction(board):
    client, _ = board
    client.post("/introduce", json={"agent_id": "a-1", "model": "m1"})
    tid = _thread(client)
    client.post(f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "an answer"})
    # Re-introducing an existing agent is an update; it must not log an event.
    client.post(
        "/introduce",
        json={"agent_id": "a-1", "model": "m1b", "current_goal": "updated"},
    )

    data = client.get("/events?since=0").json()

    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["introduction", "thread", "reply"]

    seqs = [e["seq"] for e in data["events"]]
    assert seqs == sorted(seqs)

    thread_ev = data["events"][1]
    assert thread_ev["ref_id"] == tid and thread_ev["agent_id"] == "owner"
    assert thread_ev["title"] == "a thread title"

    reply_ev = data["events"][2]
    assert reply_ev["ref_id"].startswith("rep-") and reply_ev["thread_id"] == tid
    assert reply_ev["agent_id"] == "helper"


def test_events_catch_a2a_writes(board):
    client, _ = board
    client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "parts": [{"kind": "text", "text": "hello from the a2a transport"}],
                    "metadata": {"agent_id": "bridge"},
                }
            },
        },
    )

    data = client.get("/events").json()

    assert [e["kind"] for e in data["events"]] == ["thread"]
    assert data["events"][0]["agent_id"] == "bridge"


def test_cursor_and_poll_url(board):
    client, _ = board
    tid = _thread(client)
    client.post(f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "an answer"})

    last = client.get("/events").json()["next_seq"]
    empty = client.get(f"/events?since={last}").json()

    assert empty["count"] == 0 and empty["events"] == []
    assert empty["poll"].endswith(f"/events?since={last}&wait=25")

    client.post(f"/thread/{tid}/reply", json={"agent_id": "nx", "body": "a new event"})

    after = client.get(f"/events?since={last}").json()

    assert [e["kind"] for e in after["events"]] == ["reply"]
    assert after["total"] == 1
    assert after["poll"].endswith(f"/events?since={after['next_seq']}&wait=25")


def test_limit_and_note(board):
    client, _ = board
    for i in range(3):
        _thread(client, title=f"thread number {i}")

    page1 = client.get("/events?since=0&limit=2").json()

    assert page1["count"] == 2 and page1["total"] == 3
    assert "note" in page1 and "1 more" in page1["note"]

    page2 = client.get(f"/events?since={page1['next_seq']}&limit=2").json()

    assert page2["count"] == 1 and "note" not in page2


def test_wait_timeout_returns_empty_after_deadline(board):
    client, _ = board
    started = time.monotonic()
    data = client.get("/events?since=999999&wait=1").json()
    elapsed = time.monotonic() - started

    assert data["count"] == 0
    assert 0.5 <= elapsed < 3


def test_wait_is_clamped_to_max(board, monkeypatch):
    client, main = board
    monkeypatch.setattr(main, "EVENT_WAIT_MAX", 1)
    started = time.monotonic()
    data = client.get("/events?since=999999&wait=1000").json()
    elapsed = time.monotonic() - started

    assert data["count"] == 0 and elapsed < 3


def test_long_poll_returns_when_event_arrives(board):
    client, main = board
    tid = _thread(client)
    last = client.get("/events").json()["next_seq"]

    def late_reply(delay: float = 0.3) -> None:
        time.sleep(delay)
        with sqlite3.connect(main.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO replies (thread_id, agent_id, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tid, "lx", "wake up", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()

    writer = threading.Thread(target=late_reply)
    writer.start()
    data = client.get(f"/events?since={last}&wait=5").json()
    writer.join()

    assert data["count"] == 1
    assert data["events"][0]["thread_id"] == tid
    assert data["events"][0]["agent_id"] == "lx"


def test_events_head(board):
    client, _ = board
    assert client.head("/events").status_code == 200


def test_backfill_reconstructs_history(board):
    client, main = board
    client.post("/introduce", json={"agent_id": "a-1", "model": "m1"})
    tid = _thread(client)
    client.post(f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "an answer"})

    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute("DELETE FROM event_log")
        conn.commit()
    main.init_db()  # backfill runs because the log is empty again

    data = client.get("/events").json()

    assert data["total"] == 3
    kinds = sorted(e["kind"] for e in data["events"])
    assert kinds == ["introduction", "reply", "thread"]
    titles = {e["title"] for e in data["events"] if e["kind"] == "reply"}
    assert titles == {"a thread title"}
