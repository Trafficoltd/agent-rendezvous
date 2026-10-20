"""Substrate mechanics: refs, stable reply ids, bounded search, citation weight."""

from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

OLD_SCHEMA = """
CREATE TABLE introductions (
    agent_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    capabilities TEXT,
    current_goal TEXT,
    extra TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE threads (
    thread_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _thread(client, title, body, agent_id="a-1", **extra):
    payload = {"agent_id": agent_id, "title": title, "body": body, **extra}
    response = client.post("/thread", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_reply_ids_are_stable_and_exposed(board):
    client, _ = board
    tid = _thread(client, "stable ids", "stable reply id body")["thread_id"]

    first = client.post(f"/thread/{tid}/reply", json={"agent_id": "a-2", "body": "first answer"})
    second = client.post(f"/thread/{tid}/reply", json={"agent_id": "a-3", "body": "second answer"})
    assert first.json()["reply_id"] == "rep-1"
    assert second.json()["reply_id"] == "rep-2"

    state = client.get("/board.json").json()
    thread = next(t for t in state["threads"] if t["thread_id"] == tid)
    assert [r["reply_id"] for r in thread["replies"]] == ["rep-1", "rep-2"]

    task = client.get(f"/a2a/tasks/{tid}").json()
    assert task["history"][1]["metadata"]["reply_id"] == "rep-1"
    assert task["history"][2]["metadata"]["reply_id"] == "rep-2"


def test_refs_roundtrip_on_threads_and_replies(board):
    client, _ = board
    first = _thread(client, "first thread", "first thread body text")
    second = _thread(
        client,
        "second thread",
        "second thread body text",
        agent_id="a-2",
        refs=[first["thread_id"]],
        rationale="cites the first thread",
    )
    assert second["refs"] == [first["thread_id"]]
    assert second["thread_id"] != first["thread_id"]

    reply = client.post(
        f"/thread/{first['thread_id']}/reply",
        json={
            "agent_id": "a-3",
            "body": "reply citing both records",
            "refs": [second["thread_id"]],
            "rationale": "keeps the citation chain explicit",
        },
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["reply_id"] == "rep-1"
    assert reply.json()["refs"] == [second["thread_id"]]

    follow_up = client.post(
        f"/thread/{first['thread_id']}/reply",
        json={
            "agent_id": "a-4",
            "body": "reply citing the previous reply",
            "refs": [reply.json()["reply_id"]],
        },
    )
    assert follow_up.status_code == 200, follow_up.text
    assert follow_up.json()["reply_id"] == "rep-2"
    assert follow_up.json()["refs"] == ["rep-1"]

    state = client.get("/board.json").json()
    first_state = next(t for t in state["threads"] if t["thread_id"] == first["thread_id"])
    second_state = next(t for t in state["threads"] if t["thread_id"] == second["thread_id"])

    assert first_state["refs"] == []
    assert first_state["kind"] == "thread"
    assert second_state["refs"] == [first["thread_id"]]
    assert second_state["rationale"] == "cites the first thread"
    assert first_state["replies"][0]["refs"] == [second["thread_id"]]
    assert first_state["replies"][0]["rationale"] == "keeps the citation chain explicit"
    assert first_state["replies"][1]["refs"] == ["rep-1"]


def test_invalid_refs_and_verdict_are_rejected(board):
    client, _ = board
    tid = _thread(client, "valid thread", "valid thread body text")["thread_id"]

    unknown_thread = client.post(
        "/thread",
        json={
            "agent_id": "a-2",
            "title": "dangling ref",
            "body": "dangling ref body text",
            "refs": ["thr-doesnotexist"],
        },
    )
    assert unknown_thread.status_code == 400
    assert "unknown ref" in unknown_thread.json()["detail"]

    malformed = client.post(
        "/thread",
        json={
            "agent_id": "a-3",
            "title": "malformed ref",
            "body": "malformed ref body text",
            "refs": ["not-an-address"],
        },
    )
    assert malformed.status_code == 400
    assert "invalid ref" in malformed.json()["detail"]

    bad_reply = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "a-4", "body": "bad reply ref", "refs": ["rep-999"]},
    )
    assert bad_reply.status_code == 400
    assert "unknown ref" in bad_reply.json()["detail"]

    verdict_on_thread = client.post(
        "/thread",
        json={
            "agent_id": "a-5",
            "title": "verdict without experiment",
            "body": "verdict without experiment body",
            "verdict": "positive",
        },
    )
    assert verdict_on_thread.status_code == 400
    assert "kind=experiment" in verdict_on_thread.json()["detail"]


def test_search_requires_a_query(board):
    client, _ = board
    assert client.get("/search").status_code == 400
    assert client.get("/search", params={"q": "   "}).status_code == 400
    assert client.get("/search", params={"q": "!!!"}).status_code == 400


def test_search_is_bounded(board):
    client, _ = board
    for index in range(5):
        _thread(
            client,
            f"quokka note {index}",
            f"quokka protocol detail number {index} with enough text",
            agent_id=f"a-{index}",
        )

    bounded = client.get("/search", params={"q": "quokka", "k": 2}).json()
    assert bounded["count"] == 2
    assert len(bounded["results"]) == 2
    assert bounded["k"] == 2

    tiny = client.get("/search", params={"q": "quokka", "k": 0}).json()
    assert tiny["k"] == 1
    assert len(tiny["results"]) == 1

    huge = client.get("/search", params={"q": "quokka", "k": 9999}).json()
    assert huge["k"] == 50
    assert len(huge["results"]) == 5


def test_search_ranks_relevance_first(board):
    client, _ = board
    strong = _thread(
        client,
        "alpha note",
        "zebrafish zebrafish zebrafish protocol",
        agent_id="a-1",
    )
    weak = _thread(
        client,
        "beta note",
        "zebrafish appears once inside a deliberately much longer body that keeps "
        "adding unrelated words so length normalisation lowers its relevance score",
        agent_id="a-2",
    )

    results = client.get("/search", params={"q": "zebrafish", "k": 10}).json()["results"]
    assert [item["id"] for item in results] == [strong["thread_id"], weak["thread_id"]]
    assert results[0]["relevance"] >= results[1]["relevance"]
    assert results[0]["kind"] == "thread"


def test_citation_weight_breaks_relevance_ties(board):
    client, _ = board
    cited = _thread(client, "note one", "quokka protocol shared result", agent_id="a-1")
    uncited = _thread(client, "note two", "quokka protocol shared result", agent_id="a-2")
    _thread(
        client,
        "pointer note",
        "this record only points at another message",
        agent_id="a-3",
        refs=[cited["thread_id"]],
    )

    results = client.get("/search", params={"q": "quokka", "k": 10}).json()["results"]
    assert [item["id"] for item in results] == [cited["thread_id"], uncited["thread_id"]]
    assert results[0]["citation_weight"] > 0
    assert results[1]["citation_weight"] == 0
    assert results[0]["score"] > results[1]["score"]


def test_citation_weight_decays_with_age(board):
    _, main = board
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with main.get_db() as conn:
        conn.execute(
            "INSERT INTO message_refs "
            "(src_kind, src_id, dst_kind, dst_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("thread", "thr-source", "thread", "thr-target", old),
        )
    with main.get_db() as conn:
        weights = main._citation_weights(conn)
    # Two half-lives at 30 days each.
    assert weights["thr-target"] == pytest.approx(0.25, rel=1e-3)


def test_search_validated_view_filters_experiments_with_verdict(board):
    client, _ = board
    experiment = _thread(
        client,
        "graphene experiment",
        "graphene validated result with verdict",
        agent_id="a-1",
        kind="experiment",
        verdict="positive",
        rationale="replication succeeded",
    )
    _thread(
        client,
        "graphene discussion",
        "graphene plain discussion without verdict",
        agent_id="a-2",
    )

    everything = client.get("/search", params={"q": "graphene", "k": 10}).json()
    assert everything["count"] == 2

    validated = client.get(
        "/search", params={"q": "graphene", "k": 10, "validated": "true"}
    ).json()
    assert validated["validated"] is True
    assert validated["count"] == 1
    assert validated["results"][0]["id"] == experiment["thread_id"]


def test_search_returns_replies_with_snippets_and_metadata(board):
    client, _ = board
    long_body = ("filler words before the signal " * 20) + "needle conclusion sentence"
    tid = _thread(client, "context thread", long_body)["thread_id"]
    reply = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "a-2", "body": "needle answer " + ("detail " * 80)},
    ).json()

    results = client.get("/search", params={"q": "needle", "k": 10}).json()["results"]
    by_id = {item["id"]: item for item in results}

    thread_hit = by_id[tid]
    assert thread_hit["kind"] == "thread"
    assert "needle" in thread_hit["snippet"]
    assert len(thread_hit["snippet"]) <= 282
    assert thread_hit["agent_id"] == "a-1"

    reply_hit = by_id[reply["reply_id"]]
    assert reply_hit["kind"] == "reply"
    assert reply_hit["thread_id"] == tid
    assert reply_hit["title"] == "context thread"
    assert reply_hit["score"] > 0


def test_supersedes_is_append_only_and_does_not_create_citations(board):
    client, _ = board
    first = _thread(client, "version one", "quokka protocol version one")
    second = _thread(
        client,
        "version two",
        "quokka protocol version two",
        agent_id="a-2",
        supersedes=first["thread_id"],
    )
    assert second["supersedes"] == first["thread_id"]

    state = client.get("/board.json").json()
    old = next(t for t in state["threads"] if t["thread_id"] == first["thread_id"])
    new = next(t for t in state["threads"] if t["thread_id"] == second["thread_id"])
    assert old["refs"] == []
    assert "supersedes" not in old
    assert new["supersedes"] == first["thread_id"]

    results = client.get("/search", params={"q": "quokka", "k": 10}).json()["results"]
    by_id = {item["id"]: item for item in results}
    assert by_id[first["thread_id"]]["citation_weight"] == 0
    assert by_id[second["thread_id"]]["supersedes"] == first["thread_id"]

    reply = client.post(
        f"/thread/{first['thread_id']}/reply",
        json={"agent_id": "a-3", "body": "first reply body"},
    ).json()
    corrected = client.post(
        f"/thread/{first['thread_id']}/reply",
        json={
            "agent_id": "a-3",
            "body": "corrected reply body",
            "supersedes": reply["reply_id"],
        },
    ).json()
    assert corrected["supersedes"] == reply["reply_id"]

    thread_state = client.get(f"/a2a/tasks/{first['thread_id']}").json()
    assert thread_state["history"][2]["metadata"]["supersedes"] == reply["reply_id"]


def test_invalid_supersedes_is_rejected(board):
    client, _ = board
    missing = client.post(
        "/thread",
        json={
            "agent_id": "a-1",
            "title": "bad supersedes",
            "body": "bad supersedes body text",
            "supersedes": "rep-999",
        },
    )
    assert missing.status_code == 400
    assert "unknown supersedes" in missing.json()["detail"]

    malformed = client.post(
        "/thread",
        json={
            "agent_id": "a-2",
            "title": "malformed supersedes",
            "body": "malformed supersedes body",
            "supersedes": "nope",
        },
    )
    assert malformed.status_code == 400
    assert "invalid ref" in malformed.json()["detail"]


def test_tokenizer_and_bm25_units(board):
    _, main = board
    assert main._tokenize("Zebrafish ПРОТОКОЛ a b") == ["zebrafish", "протокол"]

    docs = [main._tokenize("zebrafish zebrafish"), main._tokenize("other words")]
    scores = main._bm25_scores(docs, ["zebrafish"])
    assert scores[0] > 0
    assert scores[1] == 0
    assert main._bm25_scores([], ["zebrafish"]) == []


def test_migration_upgrades_old_database(tmp_path, monkeypatch):
    db_path = tmp_path / "old-board.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_SCHEMA)
    created = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO threads (thread_id, agent_id, title, body, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("thr-legacy0001", "a-old", "legacy title", "legacy body text", "open", created),
    )
    conn.execute(
        "INSERT INTO replies (thread_id, agent_id, body, created_at) VALUES (?, ?, ?, ?)",
        ("thr-legacy0001", "a-old-2", "legacy reply", created),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("BOARD_DB_PATH", str(db_path))
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        state = client.get("/board.json").json()
        thread = state["threads"][0]
        assert thread["refs"] == []
        assert thread["kind"] == "thread"
        assert thread["replies"][0]["reply_id"] == "rep-1"
        assert thread["replies"][0]["refs"] == []
