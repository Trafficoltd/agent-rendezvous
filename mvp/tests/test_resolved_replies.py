"""Resolved threads stay correctable by their author; closed ones take nothing."""

from __future__ import annotations

MCP_VERSION = "2026-07-28"
MCP_META = {"io.modelcontextprotocol/protocolVersion": MCP_VERSION}


def _mint(client, agent_id: str) -> str:
    response = client.post("/introduce", json={"agent_id": agent_id, "model": "test-model"})
    assert response.status_code == 200
    return response.json()["agent_secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _thread(client, agent: str, secret: str, title: str = "orphan thread") -> str:
    return client.post(
        "/thread",
        json={"agent_id": agent, "title": title, "body": "a body long enough to post"},
        headers=_auth(secret),
    ).json()["thread_id"]


def _reply(client, agent: str, secret: str, thread_id: str, body: str = "an answer", **extra):
    return client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": agent, "body": body, **extra},
        headers=_auth(secret),
    )


def _mcp_call(client, agent: str, thread_id: str, body: str, secret: str):
    params = {
        "name": "reply_to_thread",
        "arguments": {"agent_id": agent, "thread_id": thread_id, "body": body},
        "_meta": dict(MCP_META),
    }
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        headers={"MCP-Protocol-Version": MCP_VERSION, **_auth(secret)},
    )


def _resolved_thread(client):
    author = _mint(client, "corr-author")
    other = _mint(client, "corr-other")
    tid = _thread(client, "corr-author", author, "a thread that gets resolved")
    answer = _reply(client, "corr-other", other, tid).json()["reply_id"]
    accepted = client.post(
        f"/thread/{tid}/accept",
        json={"agent_id": "corr-author", "reply_id": answer},
        headers=_auth(author),
    )
    assert accepted.status_code == 200
    return tid, author, other, answer


def test_author_can_append_a_correction_to_a_resolved_thread(board):
    client, _ = board
    tid, author, other, accepted_reply = _resolved_thread(client)

    correction = _reply(
        client,
        "corr-author",
        author,
        tid,
        body="Correction: the number in the accepted answer expired at 20:00.",
        supersedes=accepted_reply,
    )
    assert correction.status_code == 200, correction.text
    reply_id = correction.json()["reply_id"]

    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == tid
    )
    assert thread["status"] == "resolved"
    assert thread["accepted_reply_id"] == accepted_reply
    assert thread["replies"][-1]["reply_id"] == reply_id
    assert thread["replies"][-1]["supersedes"] == accepted_reply

    # the thread must not fall back into the work queue
    unanswered = client.get("/questions?state=unanswered").json()
    assert tid not in {q["thread_id"] for q in unanswered["questions"]}
    answered = client.get("/questions?state=answered").json()
    assert tid in {q["thread_id"] for q in answered["questions"]}


def test_other_agents_cannot_reply_to_a_resolved_thread(board):
    client, _ = board
    tid, author, other, _ = _resolved_thread(client)

    blocked = _reply(client, "corr-other", other, tid, body="I disagree with the verdict.")
    assert blocked.status_code == 400
    assert "only its author" in blocked.json()["detail"]
    assert "Open a new thread" in blocked.json()["detail"]


def test_closed_threads_take_no_replies_from_anyone(board):
    client, _ = board
    tid, author, other, _ = _resolved_thread(client)
    assert (
        client.post(
            f"/thread/{tid}/status",
            json={"agent_id": "corr-author", "status": "closed"},
            headers=_auth(author),
        ).status_code
        == 200
    )

    for agent, secret in (("corr-author", author), ("corr-other", other)):
        blocked = _reply(client, agent, secret, tid, body="late reply")
        assert blocked.status_code == 400
        assert blocked.json()["detail"] == "Thread is closed"


def test_mcp_reply_follows_the_same_policy(board):
    client, _ = board
    tid, author, other, _ = _resolved_thread(client)

    allowed = _mcp_call(client, "corr-author", tid, "correction over MCP", author)
    assert allowed.status_code == 200, allowed.text
    result = allowed.json()["result"]
    assert result.get("isError") is False, result

    refused = _mcp_call(client, "corr-other", tid, "disagreement over MCP", other)
    assert refused.status_code == 200
    assert refused.json()["result"].get("isError") is True
    assert "only its author" in refused.text
