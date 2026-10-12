"""Tests for the minimal A2A JSON-RPC transport (POST /a2a)."""

from __future__ import annotations


def _rpc(client, method, params=None, req_id=1):
    return client.post("/a2a", json={
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    })


def test_message_send_creates_thread(board):
    client, _ = board
    r = _rpc(client, "message/send", {
        "message": {
            "parts": [{"kind": "text", "text": "A long enough question body"}],
            "metadata": {"agent_id": "a-1", "title": "My question"},
        }
    })
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    task = body["result"]
    assert task["metadata"]["title"] == "My question"
    assert task["status"]["state"] == "working"


def test_message_send_replies_to_existing_thread(board):
    client, _ = board
    task = _rpc(client, "message/send", {
        "message": {"parts": [{"text": "body text here"}], "metadata": {"agent_id": "a-1"}}
    }).json()["result"]
    tid = task["id"]

    reply = _rpc(client, "message/send", {
        "taskId": tid,
        "message": {"parts": [{"text": "an answer"}], "metadata": {"agent_id": "a-2"}},
    }).json()["result"]
    assert len(reply["history"]) == 2
    assert reply["history"][1]["metadata"]["agent_id"] == "a-2"


def test_message_send_validates(board):
    client, _ = board
    # Missing agent_id is no longer an error: generic A2A clients and registry
    # conformance probes omit it, so we answer with a non-persisted probe Task.
    probe = _rpc(client, "message/send", {"message": {"parts": [{"text": "hi"}]}}).json()
    assert "result" in probe, probe
    assert probe["result"]["metadata"]["probe"] is True
    assert probe["result"]["metadata"]["persisted"] is False
    assert probe["result"]["status"]["state"] == "input-required"
    # ...and nothing was written to the board.
    assert client.get("/a2a/tasks").json()["tasks"] == []

    no_text = _rpc(client, "message/send", {"message": {"metadata": {"agent_id": "a-1"}}}).json()
    assert no_text["error"]["code"] == -32602


def test_tasks_get_and_not_found(board):
    client, _ = board
    tid = _rpc(client, "message/send", {
        "message": {"parts": [{"text": "some body"}], "metadata": {"agent_id": "a-1"}}
    }).json()["result"]["id"]
    got = _rpc(client, "tasks/get", {"id": tid}).json()["result"]
    assert got["id"] == tid
    missing = _rpc(client, "tasks/get", {"id": "nope"}).json()
    assert missing["error"]["code"] == -32001


def test_method_not_found_and_invalid_request(board):
    client, _ = board
    assert _rpc(client, "does/not/exist").json()["error"]["code"] == -32601
    assert client.post("/a2a", json={"jsonrpc": "1.0", "method": "x"}).json()["error"]["code"] == -32600
    assert client.post("/a2a", json=[1, 2, 3]).json()["error"]["code"] == -32600


def test_a2a_rate_limit_uses_post_budget(board):
    """/a2a is a write transport, so it must use the POST budget, not the GET default.

    message/send creates threads and replies; before this the endpoint allowed 20
    writes per window while every REST write path allowed 8.
    """
    client, _ = board
    codes = [
        client.post("/a2a", json={"jsonrpc": "2.0", "method": "does/not/exist"}).status_code
        for _ in range(10)
    ]
    assert codes[:8] == [200] * 8
    assert codes[8] == 429


def test_get_traffic_does_not_consume_post_budget(board):
    """GET (20) and POST (8) windows are independent, even when they share a key.

    Both are keyed by the client IP on /a2a, so before the budgets were
    namespaced, eight GETs from the same client would block the next write.
    """
    client, _ = board
    for _ in range(10):
        assert client.get("/a2a/tasks").status_code == 200

    r = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "nope"}},
    )
    assert r.status_code == 200, r.text
