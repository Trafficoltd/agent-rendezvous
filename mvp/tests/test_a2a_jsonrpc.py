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
    no_agent = _rpc(client, "message/send", {"message": {"parts": [{"text": "hi"}]}}).json()
    assert no_agent["error"]["code"] == -32602
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


def test_a2a_rate_limit_by_ip(board):
    client, _ = board
    codes = [
        client.post("/a2a", json={"jsonrpc": "2.0", "method": "does/not/exist"}).status_code
        for _ in range(21)
    ]
    assert codes[:20] == [200] * 20
    assert codes[20] == 429
