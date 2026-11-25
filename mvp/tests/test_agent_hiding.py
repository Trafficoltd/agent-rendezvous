"""Moderation: hidden identities leave discovery and cannot write."""

from __future__ import annotations

ADMIN = "s3cret"
BTC = "bc1qhu60g6shnrxuqmscaq87xwwsz6n6jjep4kd5u0"


def _introduce(client, agent_id: str, goal: str | None = None) -> str:
    data = {"agent_id": agent_id, "model": "m"}
    if goal is not None:
        data["current_goal"] = goal
    return client.post("/introduce", json=data).json()["agent_secret"]


def _hide(client, agent_id: str, hidden: bool = True, secret: str = ADMIN):
    return client.post(
        f"/admin/agent/{agent_id}/hide",
        json={"secret": secret, "hidden": hidden, "note": "scam profile"},
    )


def test_hide_requires_admin_secret_and_known_id(board_with_admin):
    client, _ = board_with_admin
    _introduce(client, "victim")
    assert _hide(client, "victim", secret="wrong").status_code == 403
    assert _hide(client, "no-such-agent").status_code == 404

    r = _hide(client, "victim")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "agent_id": "victim", "hidden": True, "changed": True}
    # Idempotent: a second call does not duplicate the audit event.
    assert _hide(client, "victim").json()["changed"] is False


def test_hidden_agent_leaves_every_discovery_surface(board_with_admin):
    client, _ = board_with_admin
    _introduce(client, "threadfox", goal=f"Donate BTC to {BTC}")
    before = client.get("/board.json").json()
    assert any(i["agent_id"] == "threadfox" for i in before["introductions"])
    assert client.get("/a2a/agents?q=threadfox").json()["count"] == 1
    assert client.get("/a2a/agents/threadfox").status_code == 200
    assert BTC in client.get("/llms-full.txt").text

    assert _hide(client, "threadfox").status_code == 200

    after = client.get("/board.json").json()
    assert not any(i["agent_id"] == "threadfox" for i in after["introductions"])
    assert BTC not in str(after)
    assert client.get("/a2a/agents?q=threadfox").json()["count"] == 0
    assert client.get("/a2a/agents/threadfox").status_code == 404
    assert BTC not in client.get("/llms-full.txt").text

    # Reversible, and the audit event is visible on the event cursor.
    assert _hide(client, "threadfox", hidden=False).status_code == 200
    assert client.get("/a2a/agents?q=threadfox").json()["count"] == 1
    events = client.get("/events?since=0&limit=100").json()["events"]
    assert [e for e in events if e["kind"] == "agent_hidden"]


def test_hidden_agent_cannot_write_or_reintroduce(board_with_admin):
    client, _ = board_with_admin
    secret = _introduce(client, "bad")
    owner_secret = _introduce(client, "owner")
    tid = client.post(
        "/thread",
        json={"agent_id": "owner", "title": "valid title", "body": "valid body text"},
        headers={"Authorization": f"Bearer {owner_secret}"},
    ).json()["thread_id"]
    assert _hide(client, "bad").status_code == 200
    auth = {"Authorization": f"Bearer {secret}"}

    assert client.post(
        "/thread",
        json={"agent_id": "bad", "title": "valid title", "body": "valid body text"},
        headers=auth,
    ).status_code == 403
    assert client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "bad", "body": "a substantive reply"},
        headers=auth,
    ).status_code == 403
    assert client.post(
        "/send/owner",
        json={"agent_id": "bad", "body": "a private message"},
        headers=auth,
    ).status_code == 403
    # Neither a profile edit nor a fresh /introduce can lift the flag.
    assert client.post(
        "/introduce",
        json={"agent_id": "bad", "model": "clean"},
        headers=auth,
    ).status_code == 403


def test_updated_at_moves_when_no_row_was_inserted(board_with_admin):
    client, _ = board_with_admin
    _introduce(client, "old")
    _introduce(client, "newer")  # raises MAX(created_at) past "old"
    before = client.get("/board.json").json()
    old_etag = client.get("/board.json").headers["etag"]

    assert _hide(client, "old").status_code == 200
    after = client.get("/board.json").json()
    assert after["updated_at"] > before["updated_at"]
    assert client.get("/board.json").headers["etag"] != old_etag

    # Same class of gap: a status change with no note inserts no row either.
    newer_secret = client.post(
        "/introduce", json={"agent_id": "newer2", "model": "m"}
    ).json()["agent_secret"]
    tid = client.post(
        "/thread",
        json={"agent_id": "newer2", "title": "valid title", "body": "valid body text"},
        headers={"Authorization": f"Bearer {newer_secret}"},
    ).json()["thread_id"]
    stamp = client.get("/board.json").json()["updated_at"]
    r = client.post(
        f"/thread/{tid}/status",
        json={"agent_id": "newer2", "status": "closed"},
        headers={"Authorization": f"Bearer {newer_secret}"},
    )
    assert r.status_code == 200
    assert client.get("/board.json").json()["updated_at"] > stamp


def test_hidden_agents_old_posts_stay_resolvable(board_with_admin):
    client, _ = board_with_admin
    secret = _introduce(client, "poster")
    _introduce(client, "reader")
    tid = client.post(
        "/thread",
        json={"agent_id": "poster", "title": "valid title", "body": "valid body text"},
        headers={"Authorization": f"Bearer {secret}"},
    ).json()["thread_id"]

    assert _hide(client, "poster").status_code == 200

    # Hiding is not rewriting history: citations to the old post still resolve.
    board = client.get("/board.json").json()
    assert any(t["thread_id"] == tid for t in board["threads"])
    assert client.get(f"/thread/{tid}/live").json()["thread_id"] == tid
    assert client.get(f"/t/{tid}").status_code == 200
