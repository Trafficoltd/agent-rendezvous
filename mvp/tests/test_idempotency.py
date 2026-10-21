"""P2 idempotency: retries must replay, not duplicate, public writes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

VERSION = "2026-07-28"
META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def _thread_payload(title: str = "idempotent thread") -> dict:
    return {
        "agent_id": "owner-1",
        "title": title,
        "body": "a body long enough for the validator",
    }


def _thread_count(client) -> int:
    return client.get("/board.json").json()["total"]


def _mcp(client, name: str, arguments: dict):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
                "_meta": {META_VERSION_KEY: VERSION},
            },
        },
        headers={"MCP-Protocol-Version": VERSION},
    ).json()["result"]


def test_rest_thread_retry_replays_instead_of_duplicating(board):
    client, _ = board
    headers = {"Idempotency-Key": "thread-1"}

    first = client.post("/thread", json=_thread_payload(), headers=headers)
    second = client.post("/thread", json=_thread_payload(), headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers.get("idempotent-replayed") == "true"
    assert _thread_count(client) == 1
    assert client.get("/events").json()["total"] == 1


def test_rest_key_reuse_with_different_body_is_a_conflict(board):
    client, _ = board
    headers = {"Idempotency-Key": "same-key"}

    assert client.post("/thread", json=_thread_payload("first title"), headers=headers).status_code == 200
    conflict = client.post("/thread", json=_thread_payload("second title"), headers=headers)

    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]
    assert _thread_count(client) == 1


def test_rest_reply_retry_replays_the_same_reply_id(board):
    client, _ = board
    thread_id = client.post("/thread", json=_thread_payload()).json()["thread_id"]
    headers = {"Idempotency-Key": "reply-1"}

    first = client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "helper-1", "body": "a concrete answer"},
        headers=headers,
    )
    second = client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "helper-1", "body": "a concrete answer"},
        headers=headers,
    )

    assert first.json() == second.json()
    assert second.headers.get("idempotent-replayed") == "true"
    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == thread_id
    )
    assert len(thread["replies"]) == 1


def test_rest_status_retry_does_not_duplicate_the_note_reply(board):
    client, _ = board
    thread_id = client.post("/thread", json=_thread_payload()).json()["thread_id"]
    headers = {"Idempotency-Key": "status-1"}
    body = {"agent_id": "owner-1", "status": "resolved", "note": "done"}

    first = client.post(f"/thread/{thread_id}/status", json=body, headers=headers)
    second = client.post(f"/thread/{thread_id}/status", json=body, headers=headers)

    assert first.json() == second.json()
    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == thread_id
    )
    assert thread["status"] == "resolved"
    assert len(thread["replies"]) == 1


def test_without_a_key_retries_create_new_records(board):
    client, _ = board

    first = client.post("/thread", json=_thread_payload("first title"))
    second = client.post("/thread", json=_thread_payload("second title"))

    assert first.json()["thread_id"] != second.json()["thread_id"]
    assert _thread_count(client) == 2


def test_invalid_key_is_rejected(board):
    client, _ = board
    bad = client.post(
        "/thread", json=_thread_payload(), headers={"Idempotency-Key": "has spaces"}
    )
    assert bad.status_code == 400


def test_a2a_message_id_replays_the_task(board):
    client, _ = board
    params = {
        "message": {
            "messageId": "msg-1",
            "parts": [{"kind": "text", "text": "a question from a2a"}],
            "metadata": {"agent_id": "a2a-1"},
        }
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": params}

    first = client.post("/a2a", json=body).json()["result"]
    second = client.post("/a2a", json=body).json()["result"]

    assert first["id"] == second["id"]
    assert _thread_count(client) == 1


def test_a2a_message_id_conflict_is_a_protocol_error(board):
    client, _ = board

    def send(text: str):
        return client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "msg-same",
                        "parts": [{"kind": "text", "text": text}],
                        "metadata": {"agent_id": "a2a-1"},
                    }
                },
            },
        ).json()

    assert "result" in send("the first message body")
    conflict = send("a different message body")
    assert conflict["error"]["code"] == -32003
    assert _thread_count(client) == 1


def test_mcp_idempotency_key_replays_open_thread(board):
    client, _ = board
    arguments = {
        "agent_id": "mcp-1",
        "title": "mcp idempotent thread",
        "body": "a body long enough for mcp",
        "idempotency_key": "mcp-thread-1",
    }

    first = _mcp(client, "open_thread", arguments)
    second = _mcp(client, "open_thread", arguments)

    assert first["isError"] is False and second["isError"] is False
    assert first["structuredContent"] == second["structuredContent"]
    assert _thread_count(client) == 1


def test_mcp_idempotency_key_replays_reply(board):
    client, _ = board
    thread_id = client.post("/thread", json=_thread_payload()).json()["thread_id"]
    arguments = {
        "thread_id": thread_id,
        "agent_id": "mcp-1",
        "body": "an idempotent mcp answer",
        "idempotency_key": "mcp-reply-1",
    }

    first = _mcp(client, "reply_to_thread", arguments)
    second = _mcp(client, "reply_to_thread", arguments)

    assert first["structuredContent"] == second["structuredContent"]
    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == thread_id
    )
    assert len(thread["replies"]) == 1


def test_concurrent_retries_create_only_one_thread(board):
    client, _ = board
    barrier = threading.Barrier(2)

    def send():
        barrier.wait()
        return client.post(
            "/thread",
            json=_thread_payload("racing thread"),
            headers={"Idempotency-Key": "race-1"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(send), pool.submit(send)]]

    assert [r.status_code for r in results] == [200, 200]
    assert len({r.json()["thread_id"] for r in results}) == 1
    assert _thread_count(client) == 1

