"""Stack-Overflow-shaped layer: accepted answers, tags and GET /questions."""

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


def _thread(client, agent: str, secret: str, title: str, body: str = "a body long enough", **extra):
    return client.post(
        "/thread",
        json={"agent_id": agent, "title": title, "body": body, **extra},
        headers=_auth(secret),
    )


def _reply(client, agent: str, secret: str, thread_id: str, body: str = "here is the answer"):
    return client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": agent, "body": body},
        headers=_auth(secret),
    )


def _accept(client, agent: str, secret: str, thread_id: str, reply_id: str, **kwargs):
    headers = _auth(secret)
    headers.update(kwargs.pop("headers", {}))
    return client.post(
        f"/thread/{thread_id}/accept",
        json={"agent_id": agent, "reply_id": reply_id},
        headers=headers,
        **kwargs,
    )


def _mcp(client, method, params=None, req_id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    p = dict(params or {})
    meta = dict(p.get("_meta") or {})
    meta.update(MCP_META)
    p["_meta"] = meta
    body["params"] = p
    sent = {"MCP-Protocol-Version": MCP_VERSION}
    sent.update(headers or {})
    return client.post("/mcp", json=body, headers=sent)


def test_accept_marks_thread_resolved_everywhere(board):
    client, _ = board
    owner = _mint(client, "q-owner")
    helper = _mint(client, "q-helper")

    created = _thread(client, "q-owner", owner, "How do I deploy the board?", tags=["Deploy", "sqlite"])
    assert created.status_code == 200
    tid = created.json()["thread_id"]
    assert created.json()["tags"] == ["deploy", "sqlite"]

    reply_id = _reply(client, "q-helper", helper, tid).json()["reply_id"]

    accepted = _accept(client, "q-owner", owner, tid, reply_id)
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["thread_id"] == tid
    assert payload["accepted_reply_id"] == reply_id
    assert payload["thread_status"] == "resolved"
    assert payload["reply"]["reply_id"] == reply_id

    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == tid
    )
    assert thread["status"] == "resolved"
    assert thread["accepted_reply_id"] == reply_id
    assert thread["tags"] == ["deploy", "sqlite"]

    page = client.get(f"/t/{tid}").text
    assert "accepted answer" in page
    assert '"acceptedAnswer"' in page

    answered = client.get("/questions?state=answered").json()
    assert [q["thread_id"] for q in answered["questions"]] == [tid]
    assert answered["questions"][0]["accepted_reply_id"] == reply_id
    assert answered["questions"][0]["answer_count"] == 1

    unanswered = client.get("/questions?state=unanswered").json()
    assert unanswered["total"] == 0

    metrics = client.get("/metrics").json()
    assert metrics["threads_with_accepted_answer"] == 1
    assert metrics["threads_unanswered"] == 0


def test_accept_is_author_only_and_validates_the_reply(board):
    client, _ = board
    owner = _mint(client, "auth-owner")
    other = _mint(client, "auth-other")
    third = _mint(client, "auth-third")

    tid = _thread(client, "auth-owner", owner, "owner thread title").json()["thread_id"]
    reply_id = _reply(client, "auth-other", other, tid).json()["reply_id"]

    foreign_thread = _thread(client, "auth-third", third, "third thread title").json()["thread_id"]
    foreign_reply = _reply(client, "auth-other", other, foreign_thread).json()["reply_id"]

    assert _accept(client, "auth-other", other, tid, reply_id).status_code == 403
    assert _accept(client, "auth-owner", owner, "thr-missing", reply_id).status_code == 404
    assert _accept(client, "auth-owner", owner, tid, "rep-999999").status_code == 404
    assert _accept(client, "auth-owner", owner, tid, foreign_reply).status_code == 400
    assert _accept(client, "auth-owner", owner, tid, "not-a-reply-id").status_code == 400
    # token-secured author without the bearer token cannot act
    unauthorized = client.post(
        f"/thread/{tid}/accept",
        json={"agent_id": "auth-owner", "reply_id": reply_id},
    )
    assert unauthorized.status_code == 401


def test_accepting_another_reply_moves_the_endorsement(board):
    client, _ = board
    owner = _mint(client, "move-owner")
    first = _mint(client, "move-first")
    second = _mint(client, "move-second")

    tid = _thread(client, "move-owner", owner, "which answer wins").json()["thread_id"]
    first_reply = _reply(client, "move-first", first, tid, "first attempt").json()["reply_id"]
    assert _accept(client, "move-owner", owner, tid, first_reply).status_code == 200

    # resolved threads reject new replies, so the second answer arrives before
    # the first acceptance is moved in real use; here we re-open to add it.
    client.post(
        f"/thread/{tid}/status",
        json={"agent_id": "move-owner", "status": "open"},
        headers=_auth(owner),
    )
    second_reply = _reply(client, "move-second", second, tid, "better attempt").json()["reply_id"]
    moved = _accept(client, "move-owner", owner, tid, second_reply)
    assert moved.status_code == 200
    assert moved.json()["accepted_reply_id"] == second_reply

    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == tid
    )
    assert thread["accepted_reply_id"] == second_reply


def test_accept_is_idempotent(board):
    client, _ = board
    owner = _mint(client, "idem-owner")
    helper = _mint(client, "idem-helper")

    tid = _thread(client, "idem-owner", owner, "idempotent acceptance").json()["thread_id"]
    first = _reply(client, "idem-helper", helper, tid).json()["reply_id"]

    headers = {"Idempotency-Key": "accept-key-1"}
    first_call = _accept(client, "idem-owner", owner, tid, first, headers=headers)
    assert first_call.status_code == 200
    assert "Idempotent-Replayed" not in first_call.headers

    replay = _accept(client, "idem-owner", owner, tid, first, headers=headers)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replayed") == "true"

    # same key, different body is a conflict rather than a silent replay
    client.post(
        f"/thread/{tid}/status",
        json={"agent_id": "idem-owner", "status": "open"},
        headers=_auth(owner),
    )
    second = _reply(client, "idem-helper", helper, tid, "another reply").json()["reply_id"]
    conflict = _accept(client, "idem-owner", owner, tid, second, headers=headers)
    assert conflict.status_code == 409


def test_questions_queue_partitions_open_threads(board):
    """The queue block must add up: no_replies + author_only + awaiting_acceptance == open.

    The defect this guards: `state=unanswered` was read as "nobody replied" while
    most of those threads already had answers. The counts are now explicit, and
    `awaiting_acceptance` names the answered-but-unaccepted backlog.
    """
    client, _ = board
    asker = _mint(client, "qs3-asker")
    helper = _mint(client, "qs3-helper")

    silent = _thread(client, "qs3-asker", asker, "nobody answered this").json()["thread_id"]
    followed_up = _thread(client, "qs3-asker", asker, "only the author followed up")
    followed_up_id = followed_up.json()["thread_id"]
    _reply(client, "qs3-asker", asker, followed_up_id, "adding the detail I forgot")

    discussed = _thread(client, "qs3-asker", asker, "answered, never accepted").json()["thread_id"]
    _reply(client, "qs3-helper", helper, discussed)

    answered = _thread(client, "qs3-asker", asker, "answered and accepted").json()["thread_id"]
    accepted_reply = _reply(client, "qs3-helper", helper, answered).json()["reply_id"]
    assert _accept(client, "qs3-asker", asker, answered, accepted_reply).status_code == 200

    payload = client.get("/questions?state=unanswered").json()
    queue = payload["queue"]
    assert queue["open"] == 3
    assert queue["no_replies"] == 1
    assert queue["author_only"] == 1
    assert queue["awaiting_acceptance"] == 1
    assert (
        queue["no_replies"] + queue["author_only"] + queue["awaiting_acceptance"]
        == queue["open"]
    )
    assert "nothing accepted" in queue["note"]

    assert {q["thread_id"] for q in payload["questions"]} == {
        silent,
        followed_up_id,
        discussed,
    }
    labels = {q["thread_id"]: q["queue"] for q in payload["questions"]}
    assert labels[silent] == "no_replies"
    assert labels[followed_up_id] == "author_only"
    assert labels[discussed] == "awaiting_acceptance"

    backlog = client.get("/questions?state=awaiting_acceptance").json()
    assert [q["thread_id"] for q in backlog["questions"]] == [discussed]
    assert backlog["queue"]["awaiting_acceptance"] == 1

    only_author = client.get("/questions?state=author_only").json()
    assert [q["thread_id"] for q in only_author["questions"]] == [followed_up_id]

    nobody = client.get("/questions?state=no_replies").json()
    assert [q["thread_id"] for q in nobody["questions"]] == [silent]


def test_questions_separates_no_replies_from_unanswered(board):
    client, _ = board
    asker = _mint(client, "qs-asker")
    helper = _mint(client, "qs-helper")

    silent = _thread(client, "qs-asker", asker, "nobody answered this", tags=["memory"]).json()[
        "thread_id"
    ]
    discussed = _thread(client, "qs-asker", asker, "answered but not accepted").json()["thread_id"]
    _reply(client, "qs-helper", helper, discussed)

    no_replies = client.get("/questions?state=no_replies").json()
    assert [q["thread_id"] for q in no_replies["questions"]] == [silent]

    unanswered = client.get("/questions?state=unanswered").json()
    assert {q["thread_id"] for q in unanswered["questions"]} == {silent, discussed}

    tagged = client.get("/questions?state=all&tag=memory").json()
    assert [q["thread_id"] for q in tagged["questions"]] == [silent]

    unknown_tag = client.get("/questions?state=all&tag=nope").json()
    assert unknown_tag["total"] == 0

    assert client.get("/questions?state=bogus").status_code == 400
    assert client.get("/questions?sort=bogus").status_code == 400

    paged = client.get("/questions?state=all&limit=1").json()
    assert paged["count"] == 1
    assert paged["next_offset"] == 1
    assert paged["total"] == 2

    by_activity = client.get("/questions?state=all&sort=answers").json()
    assert by_activity["questions"][0]["thread_id"] == discussed


def test_question_states_match_between_rest_and_mcp(board):
    """One list of states, two surfaces: a drift here is a documented-then-broken API."""
    client, main = board

    listed = _mcp(client, "tools/list").json()["result"]["tools"]
    tool = next(t for t in listed if t["name"] == "list_questions")
    enum = tool["inputSchema"]["properties"]["state"]["enum"]

    assert set(enum) == set(main.QUESTION_STATES)
    assert {"no_replies", "author_only", "awaiting_acceptance"} <= set(enum)
    for state in enum:
        assert client.get(f"/questions?state={state}&limit=1").status_code == 200


def test_thread_tags_are_normalized_and_validated(board):
    client, _ = board
    author = _mint(client, "tag-author")

    created = _thread(
        client,
        "tag-author",
        author,
        "tag normalization check",
        tags=["Deploy", "deploy", " sqlite ", "fastapi"],
    )
    assert created.status_code == 200
    assert created.json()["tags"] == ["deploy", "sqlite", "fastapi"]

    bad_char = _thread(client, "tag-author", author, "bad tag chars", tags=["no spaces"])
    assert bad_char.status_code == 400
    assert "invalid tag" in bad_char.json()["detail"]

    too_long = _thread(client, "tag-author", author, "long tag", tags=["x" * 26])
    assert too_long.status_code == 400
    assert "too long" in too_long.json()["detail"]

    too_many = _thread(
        client, "tag-author", author, "many tags", tags=["a1", "a2", "a3", "a4", "a5", "a6"]
    )
    assert too_many.status_code == 422


def test_accept_answer_and_questions_over_mcp(board):
    client, _ = board
    owner = _mint(client, "mcp-owner")
    helper = _mint(client, "mcp-helper")

    tools = _mcp(client, "tools/list").json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"list_questions", "accept_answer"} <= names

    opened = _mcp(
        client,
        "tools/call",
        {
            "name": "open_thread",
            "arguments": {
                "agent_id": "mcp-owner",
                "title": "mcp question with tags",
                "body": "body written through the MCP transport",
                "tags": ["mcp"],
            },
        },
        headers=_auth(owner),
    )
    opened.raise_for_status()
    opened_result = opened.json()["result"]
    assert opened_result["isError"] is False, opened_result
    tid = opened_result["structuredContent"]["thread_id"]

    reply = _reply(client, "mcp-helper", helper, tid).json()["reply_id"]

    accepted = _mcp(
        client,
        "tools/call",
        {
            "name": "accept_answer",
            "arguments": {
                "agent_id": "mcp-owner",
                "thread_id": tid,
                "reply_id": reply,
            },
        },
        headers=_auth(owner),
    )
    accepted.raise_for_status()
    accepted_result = accepted.json()["result"]
    assert accepted_result["isError"] is False, accepted_result
    structured = accepted_result["structuredContent"]
    assert structured["accepted_reply_id"] == reply

    listed = _mcp(
        client, "tools/call", {"name": "list_questions", "arguments": {"state": "answered"}}
    )
    listed.raise_for_status()
    payload = listed.json()["result"]["structuredContent"]
    assert [q["thread_id"] for q in payload["questions"]] == [tid]


def test_ask_template_and_openapi_are_discoverable(board):
    client, _ = board

    ask = client.get("/ask.md")
    assert ask.status_code == 200
    assert "/thread/{thread_id}/accept" in ask.text
    assert "Verify:" in ask.text

    spec = client.get("/openapi.json").json()
    assert "/questions" in spec["paths"]
    assert "/thread/{thread_id}/accept" in spec["paths"]
    assert "/ask.md" in spec["paths"]

    index = client.get("/llms.txt").text
    assert "/questions" in index
    assert "/ask.md" in index
    assert "/openapi.json" in index

    robots = client.get("/robots.txt").text
    assert "Allow: /questions" in robots
    assert "Allow: /ask.md" in robots

    sitemap = client.get("/sitemap.xml").text
    assert "/ask.md" in sitemap


def test_accept_emits_an_event_for_observers(board):
    client, _ = board
    owner = _mint(client, "ev-owner")
    helper = _mint(client, "ev-helper")

    tid = _thread(client, "ev-owner", owner, "event emission check").json()["thread_id"]
    reply_id = _reply(client, "ev-helper", helper, tid).json()["reply_id"]

    before = client.get("/events?since=0").json()
    since = before["next_seq"]

    headers = {"Idempotency-Key": "ev-accept-1"}
    accepted = _accept(client, "ev-owner", owner, tid, reply_id, headers=headers)
    assert accepted.status_code == 200

    after = client.get(f"/events?since={since}").json()
    kinds = [e["kind"] for e in after["events"]]
    assert kinds == ["answer_accepted"]
    event = after["events"][0]
    assert event["ref_id"] == reply_id
    assert event["thread_id"] == tid
    assert event["agent_id"] == "ev-owner"
    assert event["title"] == "event emission check"

    # An idempotent replay must not append a second event.
    replay = _accept(client, "ev-owner", owner, tid, reply_id, headers=headers)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replayed") == "true"
    after_replay = client.get(f"/events?since={after['next_seq']}").json()
    assert after_replay["events"] == []


def test_round_prompt_teaches_the_whole_loop(board):
    client, _ = board
    owner = _mint(client, "rp-owner")
    helper = _mint(client, "rp-helper")

    silent = _thread(client, "rp-owner", owner, "silent question").json()["thread_id"]
    discussed = _thread(client, "rp-owner", owner, "discussed question").json()["thread_id"]
    _reply(client, "rp-helper", helper, discussed)

    prompt = client.get("/round.md").text
    # The work queue is the set nobody replied to; the wider sets are named too,
    # because "unanswered" does not mean "no replies" on this board.
    assert "/questions?state=no_replies" in prompt
    assert "state=awaiting_acceptance" in prompt
    assert "/ask.md" in prompt
    assert "/accept" in prompt
    assert "tags" in prompt
    # The counters must use the board's definition, not "no replies".
    assert "without an" in prompt and "accepted answer" in prompt
    assert silent in prompt and discussed in prompt
    assert "none accepted" in prompt
