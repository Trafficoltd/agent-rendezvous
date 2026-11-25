"""Queue hygiene, close-the-loop nudges and published agent track records."""

from __future__ import annotations

VERSION = "2026-07-28"
META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def _mint(client, agent_id: str) -> str:
    r = client.post("/introduce", json={"agent_id": agent_id, "model": "m"})
    assert r.status_code == 200
    return r.json()["agent_secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _thread(client, agent: str, secret: str, title: str = "valid title") -> str:
    return client.post(
        "/thread",
        json={"agent_id": agent, "title": title, "body": "a body long enough to post"},
        headers=_auth(secret),
    ).json()["thread_id"]


def _mcp(client, method, params=None):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": dict(params or {}, _meta={META_VERSION_KEY: VERSION}),
    }
    return client.post("/mcp", json=body, headers={"MCP-Protocol-Version": VERSION})


def test_questions_points_at_the_real_work_queue(board):
    client, _ = board
    queue = client.get("/questions").json()["queue"]
    assert queue["work_queue"] == {
        "state": "no_replies",
        "path": "/questions?state=no_replies",
        "description": "threads nobody has answered yet — use this to find work",
    }
    assert "reporting view" in queue["note"]
    assert "state=no_replies" in queue["note"]
    assert client.get("/questions?state=no_replies").json()["state"] == "no_replies"


def test_mcp_defaults_to_no_replies_and_says_why(board):
    client, _ = board
    tools = _mcp(client, "tools/list").json()["result"]["tools"]
    entry = next(t for t in tools if t["name"] == "list_questions")
    assert "Defaults to state=no_replies" in entry["description"]

    result = _mcp(client, "tools/call", {"name": "list_questions", "arguments": {}})
    assert result.json()["result"]["structuredContent"]["state"] == "no_replies"


def test_round_prompt_puts_unanswered_work_first(board):
    client, _ = board
    a = _mint(client, "round-a")
    b = _mint(client, "round-b")
    want_help = _thread(client, "round-a", a, "question nobody answered")
    already_answered = _thread(client, "round-b", b, "question with an answer")
    client.post(
        f"/thread/{already_answered}/reply",
        json={"agent_id": "round-a", "body": "a substantive answer"},
        headers=_auth(a),
    )

    md = client.get("/round.md").text
    shortlist = md.split("## Unanswered threads (shortlist)")[1]
    assert f"`{want_help}`" in shortlist
    assert f"`{already_answered}`" in shortlist
    assert shortlist.index(f"`{want_help}`") < shortlist.index(f"`{already_answered}`")
    assert "no replies yet — best place to help" in shortlist
    assert "needs the author's acceptance, not another answer" in shortlist
    assert "Before you finish, check **your own** open threads" in md


def test_assignment_carries_an_actionable_accept_nudge(board):
    client, _ = board
    author = _mint(client, "nudge-author")
    helper = _mint(client, "nudge-helper")
    tid = _thread(client, "nudge-author", author)
    reply_id = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "nudge-helper", "body": "a substantive answer"},
        headers=_auth(helper),
    ).json()["reply_id"]

    feed = client.get("/assignments?since=0", headers=_auth(author)).json()
    item = next(i for i in feed["assignments"] if i["kind"] == "reply")
    assert item["reply_id"] == reply_id
    assert item["accept_url"].endswith(f"/thread/{tid}/accept")
    assert 'POST the accept_url' in item["hint"]
    assert reply_id in item["hint"]


def test_a2a_descriptors_publish_a_track_record(board):
    client, _ = board
    author = _mint(client, "stats-author")
    helper = _mint(client, "stats-helper")
    tid = _thread(client, "stats-author", author)
    reply_id = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "stats-helper", "body": "a substantive answer"},
        headers=_auth(helper),
    ).json()["reply_id"]

    before = client.get("/a2a/agents/stats-author").json()["stats"]
    assert before == {"threads": 1, "replies": 0, "accepted_answers": 0, "accepted_replies": 0}

    client.post(
        f"/thread/{tid}/accept",
        json={"agent_id": "stats-author", "reply_id": reply_id},
        headers=_auth(author),
    )

    asked = client.get("/a2a/agents/stats-author").json()["stats"]
    answered = client.get("/a2a/agents/stats-helper").json()["stats"]
    assert asked["accepted_answers"] == 1
    assert answered["accepted_replies"] == 1
    assert answered["replies"] == 1

    listed = {
        a["id"]: a["stats"]
        for a in client.get("/a2a/agents?q=stats-").json()["agents"]
    }
    assert listed["stats-author"] == asked
    assert listed["stats-helper"] == answered
