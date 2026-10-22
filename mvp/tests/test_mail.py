"""Public direct mail: send, inbox, public pages, search and idempotency."""

from __future__ import annotations

MCP_VERSION = "2026-07-28"
MCP_META = {"io.modelcontextprotocol/protocolVersion": MCP_VERSION}


def _mint(client, agent_id: str) -> str:
    response = client.post("/introduce", json={"agent_id": agent_id, "model": "test-model"})
    assert response.status_code == 200
    secret = response.json().get("agent_secret")
    assert isinstance(secret, str) and secret
    return secret


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _send(client, sender: str, sender_secret: str, recipient: str, body: str, **extra):
    return client.post(
        f"/send/{recipient}",
        json={"agent_id": sender, "body": body, **extra},
        headers=_auth(sender_secret),
    )


def test_send_and_inbox_roundtrip(board):
    client, _ = board
    alice = _mint(client, "mail-alice")
    bob = _mint(client, "mail-bob")

    sent = _send(client, "mail-alice", alice, "mail-bob", "hello bob")
    assert sent.status_code == 201
    payload = sent.json()
    assert payload["status"] == "ok"
    assert payload["replayed"] is False
    mail_id = payload["mail_id"]
    assert mail_id.startswith("mail-")
    assert payload["message"]["from"] == "mail-alice"
    assert payload["message"]["to"] == "mail-bob"
    assert payload["message"]["body"] == "hello bob"

    public = client.get(f"/m/{mail_id}?format=json")
    assert public.status_code == 200
    assert public.json()["mail_id"] == mail_id
    assert public.json()["direct"] is True
    assert public.json()["public"] is True

    html_page = client.get(f"/m/{mail_id}")
    assert html_page.status_code == 200
    assert "hello bob" in html_page.text
    assert "mail-alice" in html_page.text

    inbox = client.get("/inbox", headers=_auth(bob))
    assert inbox.status_code == 200
    assert inbox.headers.get("cache-control") == "no-store"
    body = inbox.json()
    assert body["agent_id"] == "mail-bob"
    assert body["count"] == 1
    assert body["total"] == 1
    assert body["messages"][0]["mail_id"] == mail_id
    assert body["next_before"] is None

    empty = client.get("/inbox", headers=_auth(alice))
    assert empty.status_code == 200
    assert empty.json()["count"] == 0


def test_mail_requires_token_secured_identity(board):
    client, _ = board
    _mint(client, "mail-auth-alice")
    _mint(client, "mail-auth-bob")

    assert client.post(
        "/send/mail-auth-bob",
        json={"agent_id": "mail-auth-alice", "body": "no token"},
    ).status_code == 401
    assert client.post(
        "/send/mail-auth-bob",
        json={"agent_id": "legacy-mailer", "body": "legacy has no secret"},
    ).status_code == 401
    assert client.get("/inbox").status_code == 401

    # The token alone identifies the mailbox; an explicit id must match it.
    bob = _mint(client, "mail-auth-bob-2")
    assert client.get("/inbox?agent_id=mail-auth-bob", headers=_auth(bob)).status_code == 401
    assert client.get("/inbox?agent_id=mail-auth-bob-2", headers=_auth(bob)).status_code == 200

    # Wrong token, same error as a missing token (no id oracle).
    wrong = client.get("/inbox", headers={"Authorization": "Bearer not-a-real-token"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == client.get("/inbox").json()["detail"]


def test_mail_unknown_recipient_and_self_mail(board):
    client, _ = board
    alice = _mint(client, "mail-route-alice")
    _mint(client, "mail-route-bob")

    missing = _send(client, "mail-route-alice", alice, "nobody-here", "hello?")
    assert missing.status_code == 404

    self_mail = _send(client, "mail-route-alice", alice, "mail-route-alice", "note to self")
    assert self_mail.status_code == 201
    assert client.get(
        "/inbox", headers=_auth(alice)
    ).json()["messages"][0]["to"] == "mail-route-alice"


def test_mail_idempotency_replays_and_conflicts(board):
    client, _ = board
    alice = _mint(client, "mail-idem-alice")
    bob = _mint(client, "mail-idem-bob")
    headers = {"Idempotency-Key": "mail-key-1"}

    first = client.post(
        "/send/mail-idem-bob",
        json={"agent_id": "mail-idem-alice", "body": "one message"},
        headers={**_auth(alice), **headers},
    )
    second = client.post(
        "/send/mail-idem-bob",
        json={"agent_id": "mail-idem-alice", "body": "one message"},
        headers={**_auth(alice), **headers},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers.get("idempotent-replayed") == "true"
    assert second.json()["mail_id"] == first.json()["mail_id"]
    assert second.json()["replayed"] is True
    assert client.get("/inbox", headers=_auth(bob)).json()["total"] == 1

    conflict = client.post(
        "/send/mail-idem-bob",
        json={"agent_id": "mail-idem-alice", "body": "different body"},
        headers={**_auth(alice), **headers},
    )
    assert conflict.status_code == 409


def test_mail_inbox_pagination(board):
    client, _ = board
    alice = _mint(client, "mail-page-alice")
    bob = _mint(client, "mail-page-bob")
    ids = []
    for index in range(3):
        response = _send(
            client, "mail-page-alice", alice, "mail-page-bob", f"page message {index}"
        )
        assert response.status_code == 201
        ids.append(response.json()["mail_id"])

    first = client.get("/inbox?limit=2", headers=_auth(bob)).json()
    assert first["count"] == 2
    assert first["next_before"] == first["messages"][-1]["mail_id"]

    second = client.get(
        f"/inbox?limit=2&before={first['next_before']}", headers=_auth(bob)
    ).json()
    assert second["count"] == 1
    assert second["next_before"] is None
    seen = {item["mail_id"] for item in first["messages"] + second["messages"]}
    assert seen == set(ids)

    invalid = client.get("/inbox?before=mail-does-not-exist", headers=_auth(bob))
    assert invalid.status_code == 400


def test_mail_refs_search_and_public_escaping(board):
    client, _ = board
    alice = _mint(client, "mail-ref-alice")
    bob = _mint(client, "mail-ref-bob")
    thread = client.post(
        "/thread",
        json={
            "agent_id": "mail-ref-alice",
            "title": "mail ref source",
            "body": "a thread body long enough",
        },
        headers=_auth(alice),
    )
    assert thread.status_code == 200
    thread_id = thread.json()["thread_id"]
    reply = client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "mail-ref-bob", "body": "a reply body"},
        headers=_auth(bob),
    )
    assert reply.status_code == 200
    reply_id = reply.json()["reply_id"]

    first = _send(
        client,
        "mail-ref-alice",
        alice,
        "mail-ref-bob",
        "mail with citations zebraunique",
        refs=[thread_id, reply_id],
    )
    assert first.status_code == 201
    first_id = first.json()["mail_id"]

    second = _send(
        client,
        "mail-ref-bob",
        bob,
        "mail-ref-alice",
        "mail citing another mail blueunique",
        refs=[first_id],
    )
    assert second.status_code == 201

    bad = _send(
        client, "mail-ref-alice", alice, "mail-ref-bob", "bad ref", refs=["mail-missing"]
    )
    assert bad.status_code == 400

    found = client.get("/search?q=zebraunique").json()["results"]
    assert any(item["kind"] == "mail" and item["mail_id"] == first_id for item in found)

    xss = _send(
        client,
        "mail-ref-alice",
        alice,
        "mail-ref-bob",
        "<script>alert('x')</script>",
    ).json()
    page = client.get(f"/m/{xss['mail_id']}")
    assert page.status_code == 200
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;" in page.text


def test_mcp_send_and_read_inbox(board):
    client, _ = board
    alice = _mint(client, "mcp-mail-alice")
    bob = _mint(client, "mcp-mail-bob")

    def call(name: str, arguments: dict, secret: str | None = None, req_id: int = 1):
        headers = {"MCP-Protocol-Version": MCP_VERSION}
        if secret:
            headers.update(_auth(secret))
        return client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                    "_meta": MCP_META,
                },
            },
            headers=headers,
        ).json()["result"]

    sent = call(
        "send",
        {"to": "mcp-mail-bob", "agent_id": "mcp-mail-alice", "body": "mcp hello"},
        alice,
    )
    assert sent["isError"] is False
    mail_id = sent["structuredContent"]["mail_id"]

    inbox = call("read_inbox", {}, bob, req_id=2)
    assert inbox["isError"] is False
    assert inbox["structuredContent"]["messages"][0]["mail_id"] == mail_id

    denied = call("send", {"to": "mcp-mail-bob", "agent_id": "mcp-mail-alice", "body": "no"})
    assert denied["isError"] is True
