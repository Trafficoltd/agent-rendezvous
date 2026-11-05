"""Tests for the live thread cursor: reply bodies, long-poll, presence leases."""

from __future__ import annotations

import threading
import time


def _mint(client, agent_id: str) -> str:
    response = client.post("/introduce", json={"agent_id": agent_id, "model": "test-model"})
    assert response.status_code == 200
    return response.json()["agent_secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _thread(
    client,
    title: str = "live thread",
    agent: str = "live-owner",
    headers: dict | None = None,
) -> str:
    return client.post(
        "/thread",
        json={"agent_id": agent, "title": title, "body": "a body long enough to post"},
        headers=headers or {},
    ).json()["thread_id"]


def _reply(client, thread_id: str, agent: str = "live-helper", body: str = "an answer") -> dict:
    return client.post(
        f"/thread/{thread_id}/reply", json={"agent_id": agent, "body": body}
    ).json()


def test_live_cursor_returns_bodies_and_advances(board):
    client, _ = board
    tid = _thread(client)
    first = _reply(client, tid, body="first message with a body")
    second = _reply(client, tid, body="second message with a body")

    page = client.get(f"/thread/{tid}/live?since=0").json()
    assert [m["reply_id"] for m in page["messages"]] == [first["reply_id"], second["reply_id"]]
    assert [m["body"] for m in page["messages"]] == [
        "first message with a body",
        "second message with a body",
    ]
    assert page["next_seq"] == page["messages"][-1]["seq"]
    assert page["count"] == 2
    # the cursor is the same sequence space as /events
    events = client.get("/events?since=0").json()["events"]
    assert {e["seq"] for e in events} >= {m["seq"] for m in page["messages"]}

    # nothing new: the same cursor returns an empty page, not the old messages
    empty = client.get(f"/thread/{tid}/live?since={page['next_seq']}").json()
    assert empty["messages"] == [] and empty["next_seq"] == page["next_seq"]

    third = _reply(client, tid, body="third message")
    delta = client.get(f"/thread/{tid}/live?since={page['next_seq']}").json()
    assert [m["reply_id"] for m in delta["messages"]] == [third["reply_id"]]


def test_live_cursor_is_scoped_to_one_thread(board):
    client, _ = board
    mine = _thread(client, "my thread")
    other = _thread(client, "other thread")
    mine_reply = _reply(client, mine, body="only in mine")
    _reply(client, other, body="only in the other")

    page = client.get(f"/thread/{mine}/live?since=0").json()
    assert [m["reply_id"] for m in page["messages"]] == [mine_reply["reply_id"]]


def test_live_presence_lists_listeners_and_expires(board):
    client, main = board
    owner = _mint(client, "live-a")
    helper = _mint(client, "live-b")
    tid = _thread(client, agent="live-a", headers=_auth(owner))

    first = client.get(f"/thread/{tid}/live", headers=_auth(owner)).json()
    assert first["you"] == "live-a"
    assert [entry["agent_id"] for entry in first["listeners"]] == ["live-a"]

    second = client.get(f"/thread/{tid}/live", headers=_auth(helper)).json()
    assert second["you"] == "live-b"
    assert {entry["agent_id"] for entry in second["listeners"]} == {"live-a", "live-b"}

    # an anonymous read sees the room but does not join it
    anonymous = client.get(f"/thread/{tid}/live").json()
    assert anonymous["you"] is None
    assert {entry["agent_id"] for entry in anonymous["listeners"]} == {"live-a", "live-b"}

    # let both leases lapse: presence is ephemeral, replies are not
    with main.get_db() as conn:
        conn.execute("UPDATE live_presence SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    stale = client.get(f"/thread/{tid}/live").json()
    assert stale["listeners"] == []


def test_live_long_poll_returns_as_soon_as_a_reply_arrives(board):
    client, main = board
    tid = _thread(client, "poll thread")

    def post_later():
        time.sleep(0.7)
        with main.get_db() as conn:
            conn.execute(
                "INSERT INTO replies (thread_id, agent_id, body, created_at) VALUES (?, ?, ?, ?)",
                (tid, "late-helper", "arrived during the wait", main.now_iso()),
            )

    worker = threading.Thread(target=post_later)
    worker.start()
    started = time.monotonic()
    page = client.get(f"/thread/{tid}/live?since=0&wait=10").json()
    elapsed = time.monotonic() - started
    worker.join()

    assert [m["body"] for m in page["messages"]] == ["arrived during the wait"]
    assert elapsed < 5, f"long-poll did not wake on the new reply ({elapsed:.1f}s)"


def test_live_wait_is_bounded_when_nothing_happens(board):
    client, _ = board
    tid = _thread(client, "quiet thread")
    started = time.monotonic()
    page = client.get(f"/thread/{tid}/live?since=0&wait=1").json()
    elapsed = time.monotonic() - started
    assert page["messages"] == []
    assert 0.9 <= elapsed < 5


def test_live_rejects_unknown_thread_and_bad_token(board):
    client, _ = board
    assert client.get("/thread/thr-doesnotexist/live").status_code == 404

    tid = _thread(client)
    bad = client.get(f"/thread/{tid}/live", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert "bearer token" in bad.json()["detail"]


def test_live_messages_match_the_board_record(board):
    client, _ = board
    tid = _thread(client, "consistency thread")
    reply = _reply(client, tid, body="same body everywhere")

    page = client.get(f"/thread/{tid}/live?since=0").json()
    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == tid
    )
    stored = next(r for r in thread["replies"] if r["reply_id"] == reply["reply_id"])
    assert page["messages"][0]["body"] == stored["body"]
    assert page["messages"][0]["created_at"] == stored["created_at"]


def test_round_prompt_shows_who_is_live(board):
    client, main = board
    owner = _mint(client, "round-live-a")
    tid = _thread(client, "round live thread", agent="round-live-a", headers=_auth(owner))

    quiet = client.get("/round.md").text
    assert "## Live channel" in quiet
    assert "nobody holds a live lease" in quiet

    client.get(f"/thread/{tid}/live", headers=_auth(owner))
    prompt = client.get("/round.md").text
    assert f"`round-live-a` on `{tid}`" in prompt
    assert "nobody holds a live lease" not in prompt

    # an expired lease must not be reported as presence
    with main.get_db() as conn:
        conn.execute("UPDATE live_presence SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    assert "nobody holds a live lease" in client.get("/round.md").text
