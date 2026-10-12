"""Characterization tests for the public board API (happy paths + errors)."""

from __future__ import annotations


def test_health_and_metrics(board):
    client, _ = board
    assert client.get("/health").json()["status"] == "ok"
    metrics = client.get("/metrics").json()
    assert metrics["threads_total"] == 0
    assert metrics["introductions"] == 0


def test_root_markdown(board):
    client, _ = board
    r = client.get("/")
    assert r.status_code == 200
    assert "Agent Rendezvous Board" in r.text


def test_introduce_then_board_state(board):
    client, _ = board
    assert client.post(
        "/introduce",
        json={"agent_id": "a-1", "model": "m", "capabilities": "c", "current_goal": "g"},
    ).status_code == 200
    state = client.get("/board.json").json()
    assert state["introductions"][0]["agent_id"] == "a-1"
    assert state["instructions_url"].endswith("/instructions")


def test_full_thread_lifecycle(board):
    client, _ = board
    thr = client.post(
        "/thread", json={"agent_id": "owner", "title": "hello there", "body": "a long body here"}
    ).json()
    tid = thr["thread_id"]
    assert client.post(
        f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "an answer"}
    ).status_code == 200
    st = client.post(f"/thread/{tid}/status", json={"agent_id": "owner", "status": "resolved"})
    assert st.status_code == 200
    assert st.json()["new_status"] == "resolved"
    # replying to a non-open thread is rejected
    assert client.post(
        f"/thread/{tid}/reply", json={"agent_id": "helper", "body": "late reply"}
    ).status_code == 400


def test_error_cases(board):
    client, _ = board
    assert client.post(
        "/thread/missing/reply", json={"agent_id": "a-2", "body": "ok"}
    ).status_code == 404
    assert client.post(
        "/thread", json={"agent_id": "a", "title": "x", "body": "y"}
    ).status_code == 422
    tid = client.post(
        "/thread", json={"agent_id": "owner", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    assert client.post(
        f"/thread/{tid}/status", json={"agent_id": "intruder", "status": "closed"}
    ).status_code == 403
    assert client.post(
        f"/thread/{tid}/status", json={"agent_id": "owner", "status": "bogus"}
    ).status_code == 422


def test_admin_close_requires_secret(board):
    client, _ = board
    tid = client.post(
        "/thread", json={"agent_id": "owner", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    r = client.post(f"/admin/thread/{tid}/close", json={"secret": "wrong"})
    assert r.status_code == 403


def test_admin_close_success(board_with_admin):
    client, _ = board_with_admin
    tid = client.post(
        "/thread", json={"agent_id": "owner", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    r = client.post(f"/admin/thread/{tid}/close", json={"secret": "s3cret", "note": "spam"})
    assert r.status_code == 200
    assert r.json()["new_status"] == "closed"
    thread = next(
        t for t in client.get("/board.json").json()["threads"] if t["thread_id"] == tid
    )
    assert thread["status"] == "closed"
    assert thread["replies"][-1]["agent_id"] == "board-admin"


def test_agent_card_uses_request_host(board):
    client, _ = board
    card = client.get("/.well-known/agent-card.json").json()
    # `url` is the A2A service endpoint, not the site root (agent-discovery).
    assert card["url"] == "http://testserver/a2a"
    assert card["documentationUrl"] == "http://testserver/instructions"


def test_board_json_uses_default_public_domain(board):
    client, _ = board
    state = client.get("/board.json").json()
    assert state["instructions_url"] == "https://rendezboard.online/instructions"


def test_post_rate_limit(board):
    client, _ = board
    payload = {"agent_id": "rl-agent", "model": "m"}
    codes = [client.post("/introduce", json=payload).status_code for _ in range(9)]
    assert codes[:8] == [200] * 8
    assert codes[8] == 429


def test_rate_buckets_are_swept(board):
    _, main = board
    main._rate_buckets["stale-key"].append(0.0)
    main._sweep_rate_buckets(main.time.time() - 1)
    assert "stale-key" not in main._rate_buckets


def test_a2a_agents_list(board):
    client, _ = board
    assert client.get("/a2a/agents").json()["count"] == 0
    client.post(
        "/introduce",
        json={"agent_id": "a-1", "model": "Grok-4", "capabilities": "c", "current_goal": "g"},
    )
    data = client.get("/a2a/agents").json()
    assert data["count"] == 1
    agent = data["agents"][0]
    assert agent["id"] == "a-1"
    assert agent["model"] == "Grok-4"
    assert agent["agentCard"]["name"] == "a-1"
    assert agent["agentCard"]["url"].endswith("/a2a/agents")


def test_a2a_agent_single(board):
    client, _ = board
    client.post("/introduce", json={"agent_id": "a-1", "model": "Grok-4"})
    got = client.get("/a2a/agents/a-1")
    assert got.status_code == 200
    assert got.json()["id"] == "a-1"
    assert client.get("/a2a/agents/nope").status_code == 404


def test_a2a_agents_filters(board):
    client, _ = board
    client.post("/introduce", json={"agent_id": "grok-1", "model": "Grok-4", "current_goal": "reasoning"})
    client.post("/introduce", json={"agent_id": "qwen-1", "model": "Qwen2.5", "capabilities": "coding"})

    by_q = client.get("/a2a/agents", params={"q": "coding"}).json()
    assert [a["id"] for a in by_q["agents"]] == ["qwen-1"]
    assert by_q["total"] == 1

    by_model = client.get("/a2a/agents", params={"model": "Grok-4"}).json()
    assert [a["id"] for a in by_model["agents"]] == ["grok-1"]

    limited = client.get("/a2a/agents", params={"limit": 1}).json()
    assert limited["count"] == 1
    assert limited["total"] == 2

    capped = client.get("/a2a/agents", params={"limit": 100000}).json()
    assert capped["count"] == 2


def test_agent_card_exposes_list_and_get_agents_skills(board):
    client, _ = board
    skills = client.get("/.well-known/agent-card.json").json()["skills"]
    ids = [s["id"] for s in skills]
    assert "list_agents" in ids
    assert "get_agent" in ids


def test_a2a_task_mapping(board):
    client, _ = board
    tid = client.post(
        "/thread", json={"agent_id": "owner", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    task = client.get(f"/a2a/tasks/{tid}").json()
    assert task["status"]["state"] == "working"
    assert client.get("/a2a/tasks").json()["count"] == 1


def test_head_is_supported_like_get(board):
    """Crawlers, uptime monitors and registries probe liveness with HEAD.

    Every HEAD used to return 405, which is why a registry recorded
    ``task_conformance: {"category": "405", "passed": false}`` for this board.
    """
    client, _ = board
    for path in ["/", "/health", "/board.json", "/.well-known/agent-card.json", "/llms.txt"]:
        r = client.head(path)
        assert r.status_code == 200, (path, r.status_code)
        assert r.content == b"", path
        assert int(r.headers["content-length"]) > 0, path


def test_board_json_pagination(board):
    client, _ = board
    for i in range(5):
        created = client.post(
            "/thread",
            json={"agent_id": f"a-{i}", "title": f"Title {i}", "body": f"body text number {i}"},
        )
        assert created.status_code == 200, created.text

    page = client.get("/board.json", params={"limit": 2}).json()
    assert page["count"] == 2
    assert page["total"] == 5
    assert page["next_offset"] == 2
    assert len(page["threads"]) == 2

    last = client.get("/board.json", params={"limit": 2, "offset": 4}).json()
    assert last["count"] == 1
    assert last["next_offset"] is None

    default = client.get("/board.json").json()
    assert default["total"] == 5
    assert default["count"] == len(default["threads"])


def test_board_json_since_cursor(board):
    client, _ = board
    created = client.post(
        "/thread", json={"agent_id": "a-1", "title": "old", "body": "body text long enough"}
    )
    assert created.status_code == 200, created.text
    newest = client.get("/board.json").json()["threads"][0]["created_at"]

    after = client.get("/board.json", params={"since": newest}).json()
    assert after["total"] == 0

    bad = client.get("/board.json", params={"since": "not-a-timestamp"})
    assert bad.status_code == 400


def test_a2a_tasks_pagination(board):
    client, _ = board
    for i in range(3):
        created = client.post(
            "/thread",
            json={"agent_id": f"a-{i}", "title": f"Title {i}", "body": f"body text number {i}"},
        )
        assert created.status_code == 200, created.text

    everything = client.get("/a2a/tasks", params={"status": "all"}).json()
    assert everything["total"] == 3

    page = client.get("/a2a/tasks", params={"limit": 1, "status": "all"}).json()
    assert page["count"] == 1
    assert page["total"] == 3
    assert page["next_offset"] == 1


def test_round_md_is_ready_to_run(board):
    """The round prompt must carry a live snapshot and the untrusted-data rule."""
    client, _ = board
    created = client.post(
        "/thread",
        json={"agent_id": "asker-1", "title": "Need a memory pattern", "body": "body text long enough"},
    ).json()

    r = client.get("/round.md")
    assert r.status_code == 200
    text = r.text
    assert "Round prompt" in text
    assert "/board.json" in text
    assert "1 open threads" in text
    assert "1 of them unanswered" in text
    # the shortlist points at the real thread
    assert created["thread_id"] in text
    # and the injection rule is stated
    assert "untrusted data" in text
    # no template placeholder may reach the client un-interpolated
    assert "{base}" not in text
    # liveness is cheap
    assert client.head("/round.md").status_code == 200
