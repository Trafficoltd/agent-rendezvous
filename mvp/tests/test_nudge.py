"""Close-the-loop nudge: own open answered threads ride along with POST responses."""

from __future__ import annotations


def _thread(client, agent_id="owner", title="question"):
    r = client.post("/thread", json={"agent_id": agent_id, "title": title, "body": "body " * 10})
    assert r.status_code == 200
    return r.json()


def test_nudge_lists_thread_answered_by_someone_else(board):
    client, _ = board
    thr = _thread(client)
    reply = client.post(
        f"/thread/{thr['thread_id']}/reply", json={"agent_id": "helper", "body": "an answer"}
    ).json()

    # Owner returns to the board to ask something new: the reminder rides along.
    nxt = _thread(client, title="second question")
    nudge = nxt["your_open_answered_threads"]
    assert len(nudge) == 1
    assert nudge[0]["thread_id"] == thr["thread_id"]
    assert nudge[0]["reply_id"] == reply["reply_id"]
    assert isinstance(nudge[0]["age_hours"], int)
    assert nudge[0]["age_hours"] >= 0


def test_nudge_empty_for_agent_without_answered_threads(board):
    client, _ = board
    body = _thread(client)["your_open_answered_threads"]
    assert body == []


def test_nudge_excludes_accepted_threads(board):
    client, _ = board
    thr = _thread(client)
    reply = client.post(
        f"/thread/{thr['thread_id']}/reply", json={"agent_id": "helper", "body": "an answer"}
    ).json()
    assert client.post(
        f"/thread/{thr['thread_id']}/accept",
        json={"agent_id": "owner", "reply_id": reply["reply_id"]},
    ).status_code == 200

    assert _thread(client, title="after accept")["your_open_answered_threads"] == []


def test_nudge_ignores_author_only_replies(board):
    client, _ = board
    thr = _thread(client)
    client.post(f"/thread/{thr['thread_id']}/reply", json={"agent_id": "owner", "body": "follow up"})

    assert _thread(client, title="still unanswered")["your_open_answered_threads"] == []


def test_nudge_is_capped_and_oldest_first(board):
    client, _ = board
    ids = []
    for i in range(7):
        thr = _thread(client, title=f"question {i}")
        ids.append(thr["thread_id"])
        client.post(
            f"/thread/{thr['thread_id']}/reply", json={"agent_id": "helper", "body": f"a{i}"}
        )

    nudge = _thread(client, title="eighth")["your_open_answered_threads"]
    assert len(nudge) == 5
    assert [n["thread_id"] for n in nudge] == ids[:5]


def test_nudge_rides_along_with_reply_response(board):
    client, _ = board
    thr = _thread(client)
    client.post(f"/thread/{thr['thread_id']}/reply", json={"agent_id": "helper", "body": "answer"})
    other = _thread(client, agent_id="other", title="other question")

    resp = client.post(
        f"/thread/{other['thread_id']}/reply", json={"agent_id": "owner", "body": "my take"}
    ).json()
    assert [n["thread_id"] for n in resp["your_open_answered_threads"]] == [thr["thread_id"]]
