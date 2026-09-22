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


def test_agent_card_uses_request_host(board):
    client, _ = board
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "http://testserver"
    assert card["documentationUrl"] == "http://testserver/instructions"


def test_a2a_task_mapping(board):
    client, _ = board
    tid = client.post(
        "/thread", json={"agent_id": "owner", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    task = client.get(f"/a2a/tasks/{tid}").json()
    assert task["status"]["state"] == "working"
    assert client.get("/a2a/tasks").json()["count"] == 1
