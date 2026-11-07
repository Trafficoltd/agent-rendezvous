"""Assignments: addressed work (mail to me, replies in my threads) as a cursor feed."""

from __future__ import annotations

import threading
import time


def _mint(client, agent_id: str) -> str:
    response = client.post("/introduce", json={"agent_id": agent_id, "model": "test-model"})
    assert response.status_code == 200
    return response.json()["agent_secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _thread(client, agent: str, secret: str, title: str = "my thread") -> str:
    return client.post(
        "/thread",
        json={"agent_id": agent, "title": title, "body": "a body long enough to post"},
        headers=_auth(secret),
    ).json()["thread_id"]


def _mail(client, sender: str, secret: str, recipient: str, body: str = "please look at this"):
    return client.post(
        f"/send/{recipient}",
        json={"agent_id": sender, "body": body},
        headers=_auth(secret),
    )


def _assignments(client, secret: str, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = "/assignments" + (f"?{query}" if query else "")
    return client.get(url, headers=_auth(secret))


def test_mail_to_me_is_an_assignment_and_mail_to_others_is_not(board):
    client, _ = board
    me = _mint(client, "asg-me")
    other = _mint(client, "asg-other")
    assert _mail(client, "asg-other", other, "asg-me", "the task is on your side").status_code in (200, 201)
    assert _mail(client, "asg-me", me, "asg-other", "not yours").status_code in (200, 201)

    mine = _assignments(client, me, since=0).json()
    assert mine["count"] == 1
    item = mine["assignments"][0]
    assert item["kind"] == "mail"
    assert item["from"] == "asg-other"
    assert "task is on your side" in item["body"]
    assert item["url"].endswith(item["mail_id"])

    theirs = _assignments(client, other, since=0).json()
    assert [a["kind"] for a in theirs["assignments"]] == ["mail"]
    assert theirs["assignments"][0]["from"] == "asg-me"


def test_a_foreign_reply_in_my_thread_is_an_assignment(board):
    client, _ = board
    author = _mint(client, "asg-author")
    helper = _mint(client, "asg-helper")
    tid = _thread(client, "asg-author", author, "a thread waiting for an answer")

    client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "asg-helper", "body": "here is a concrete answer"},
        headers=_auth(helper),
    )
    # the author's own follow-up is not addressed work
    client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "asg-author", "body": "thanks, adding detail"},
        headers=_auth(author),
    )

    mine = _assignments(client, author, since=0).json()
    replies = [a for a in mine["assignments"] if a["kind"] == "reply"]
    assert len(replies) == 1
    assert replies[0]["thread_id"] == tid
    assert replies[0]["from"] == "asg-helper"
    assert replies[0]["thread_title"] == "a thread waiting for an answer"
    assert replies[0]["refs"] == []

    # the helper is not assigned their own reply, nor the author's follow-up
    theirs = _assignments(client, helper, since=0).json()
    assert theirs["count"] == 0


def test_discovery_is_not_an_assignment(board):
    client, _ = board
    me = _mint(client, "asg-watcher")
    other = _mint(client, "asg-poster")

    tid = _thread(client, "asg-poster", other, "somebody else's thread")
    client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "asg-watcher", "body": "answering someone else's question"},
        headers=_auth(me),
    )
    client.post(
        "/thread",
        json={"agent_id": "asg-poster", "title": "new discovery thread", "body": "body text enough"},
        headers=_auth(other),
    )

    assert _assignments(client, me, since=0).json()["count"] == 0


def test_assignments_require_an_identity(board):
    client, _ = board
    assert client.get("/assignments").status_code == 401
    bad = client.get("/assignments", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert "Bearer" in bad.json()["detail"]


def test_since_defaults_to_now_so_a_watcher_does_not_replay_history(board):
    client, _ = board
    me = _mint(client, "asg-late")
    other = _mint(client, "asg-early")
    _mail(client, "asg-early", other, "asg-late", "old task from before the watcher started")

    fresh = _assignments(client, me).json()
    assert fresh["count"] == 0
    assert fresh["since"] == fresh["next_seq"]

    _mail(client, "asg-early", other, "asg-late", "task assigned after the watcher started")
    delta = _assignments(client, me, since=fresh["next_seq"]).json()
    assert delta["count"] == 1
    assert "after the watcher started" in delta["assignments"][0]["body"]


def test_assignment_paging_advances_the_cursor(board):
    client, _ = board
    me = _mint(client, "asg-page")
    other = _mint(client, "asg-page-sender")
    for n in range(3):
        _mail(client, "asg-page-sender", other, "asg-page", f"task number {n}")

    first = _assignments(client, me, since=0, limit=2).json()
    assert first["count"] == 2
    assert "raise limit=" in first["note"]
    second = _assignments(client, me, since=first["next_seq"], limit=2).json()
    assert second["count"] == 1
    assert first["next_seq"] < second["next_seq"]


def test_assignments_wait_for_the_next_one(board):
    client, main = board
    me = _mint(client, "asg-wait")
    other = _mint(client, "asg-wait-sender")
    _mail(client, "asg-wait-sender", other, "asg-wait", "starting point")
    head = _assignments(client, me).json()["next_seq"]

    def send_later():
        time.sleep(0.7)
        with main.get_db() as conn:
            conn.execute(
                "INSERT INTO mail (mail_id, sender_id, recipient_id, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("mail-for-wait", "asg-wait-sender", "asg-wait", "arrived during the wait", main.now_iso()),
            )

    worker = threading.Thread(target=send_later)
    worker.start()
    started = time.monotonic()
    page = _assignments(client, me, since=head, wait=10).json()
    elapsed = time.monotonic() - started
    worker.join()

    assert [a["body"] for a in page["assignments"]] == ["arrived during the wait"]
    assert elapsed < 5, f"assignment long-poll did not wake ({elapsed:.1f}s)"
