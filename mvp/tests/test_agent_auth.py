"""Identity-token tests: agent_secret minting, Bearer enforcement, legacy pass-through."""

from __future__ import annotations

import uuid

MINT = {"agent_id": "sec-1", "model": "m"}


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def test_first_introduce_mints_secret(board):
    client, _ = board
    r = client.post("/introduce", json=MINT)
    assert r.status_code == 200
    secret = r.json().get("agent_secret")
    assert isinstance(secret, str) and len(secret) >= 32


def test_second_introduce_requires_token_and_updates(board):
    client, _ = board

    data = {"agent_id": "sec-2", "model": "v1", "current_goal": "goal-1"}
    resp = client.post("/introduce", json=data).json()
    secret = resp["agent_secret"]

    # No token → update refused (profile takeover closed)
    clash = client.post(
        "/introduce", json={"agent_id": "sec-2", "model": "HIJACKED"}
    )
    assert clash.status_code == 401
    agents = client.get("/a2a/agents?q=sec-2").json()
    assert agents["agents"][0]["model"] == "v1"

    # Wrong token → refused
    wrong = client.post(
        "/introduce",
        json={"agent_id": "sec-2", "model": "HIJACKED"},
        headers={"Authorization": f"Bearer {uuid.uuid4().hex}"},
    )
    assert wrong.status_code == 401  # constant-time path, no oracle

    # Correct token → update allowed; secret is never re-exposed
    ok = client.post(
        "/introduce", json={"agent_id": "sec-2", "model": "v2"}, headers=_auth(secret)
    )
    assert ok.status_code == 200
    body = ok.json()
    assert "agent_secret" not in body
    assert body.get("agent_secret_set") is True
    assert client.get("/a2a/agents?q=sec-2").json()["agents"][0]["model"] == "v2"


def test_secured_id_blocks_impersonated_writes(board):
    client, _ = board
    secret = client.post(
        "/introduce", json={"agent_id": "sec-3", "model": "m"}
    ).json()["agent_secret"]

    # Thread under a secured id without the token
    r = client.post(
        "/thread", json={"agent_id": "sec-3", "title": "a valid title", "body": "a valid body"}
    )
    assert r.status_code == 401
    secured = client.post(
        "/thread",
        json={"agent_id": "sec-3", "title": "a valid title", "body": "a valid body"},
        headers=_auth(secret),
    )
    assert secured.status_code == 200
    tid = secured.json()["thread_id"]

    # Reply under a secured id without the token
    no_auth = client.post(
        f"/thread/{tid}/reply", json={"agent_id": "sec-3", "body": "taking over"}
    )
    assert no_auth.status_code == 401
    with_auth = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "sec-3", "body": "legit continuation"},
        headers=_auth(secret),
    )
    assert with_auth.status_code == 200

    # Status change: only the author with the token
    intruder_403 = client.post(
        f"/thread/{tid}/status", json={"agent_id": "helper", "status": "closed"}
    )
    assert intruder_403.status_code == 403
    author_no_token = client.post(
        f"/thread/{tid}/status", json={"agent_id": "sec-3", "status": "resolved"}
    )
    assert author_no_token.status_code == 401
    author_with_token = client.post(
        f"/thread/{tid}/status",
        json={"agent_id": "sec-3", "status": "resolved"},
        headers=_auth(secret),
    )
    assert author_with_token.status_code == 200


def test_legacy_ids_unchanged(board):
    """No secret ever minted → behaviour identical to pre-token API."""
    client, _ = board
    thr = client.post(
        "/thread", json={"agent_id": "legacy-1", "title": "a valid title", "body": "a valid body"}
    )
    assert thr.status_code == 200
    tid = thr.json()["thread_id"]
    assert client.post(
        f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "an answer"}
    ).status_code == 200
    assert client.post(
        f"/thread/{tid}/status", json={"agent_id": "legacy-1", "status": "resolved"}
    ).status_code == 200
    # Legacy profile update still works without any token
    assert client.post(
        "/introduce", json={"agent_id": "legacy-1", "model": "m2"}
    ).status_code == 200


def test_secret_hash_never_leaks_into_public_state(board):
    client, _ = board
    client.post(
        "/introduce",
        json={"agent_id": "leak-1", "model": "m", "extra": {"k": "v"}},
    )
    state = client.get("/board.json").json()
    intro = next(i for i in state["introductions"] if i["agent_id"] == "leak-1")
    assert "secret_hash" not in intro
    agent = client.get("/a2a/agents/leak-1").json()
    assert "secret_hash" not in agent


def test_mcp_introduce_mints_and_enforces(board):
    client, _ = board
    proto = {"MCP-Protocol-Version": "2026-07-28"}
    meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}

    def call(name, arguments, headers, req_id):
        body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, **meta},
        }
        return client.post("/mcp", json=body, headers=headers)

    mint = call(
        "introduce", {"agent_id": "mcp-1", "model": "m"}, proto, 1
    ).json()["result"]
    assert mint["isError"] is False
    secret_text = mint["content"][0]["text"]
    assert "agent_secret" in secret_text

    # No token → isError result
    second = call(
        "introduce", {"agent_id": "mcp-1", "model": "m2"}, proto, 2
    ).json()
    assert second["id"] == 2
    assert second["result"]["isError"] is True
    assert "token-secured" in second["result"]["content"][0]["text"]

    # Wrong token → isError result too
    bad = call(
        "introduce",
        {"agent_id": "mcp-1", "model": "m3"},
        {**proto, "Authorization": f"Bearer {uuid.uuid4().hex}"},
        3,
    ).json()
    assert bad["id"] == 3
    assert bad["result"]["isError"] is True


def test_a2a_message_send_requires_token_for_secured_id(board):
    client, _ = board
    secret = client.post(
        "/introduce", json={"agent_id": "a2a-sec", "model": "m"}
    ).json()["agent_secret"]

    def rpc(agent_id, headers=None):
        body = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "message/send",
            "params": {
                "message": {
                    "parts": [{"kind": "text", "text": "a body long enough"}],
                    "metadata": {"agent_id": agent_id, "title": "A question"},
                }
            },
        }
        return client.post("/a2a", json=body, headers=headers)

    denied = rpc("a2a-sec").json()
    assert denied["error"]["code"] == -32002
    allowed = rpc("a2a-sec", headers=_auth(secret)).json()
    assert "result" in allowed
