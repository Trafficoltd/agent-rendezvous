"""Payload hygiene: bounded profile metadata, conditional GET, compression."""

from __future__ import annotations

import json


def _mint(client, agent_id: str) -> str:
    response = client.post("/introduce", json={"agent_id": agent_id, "model": "test-model"})
    assert response.status_code == 200
    return response.json()["agent_secret"]


def test_oversized_extra_is_rejected_with_a_useful_message(board):
    client, _ = board
    secret = _mint(client, "fat-extra")
    response = client.post(
        "/introduce",
        json={
            "agent_id": "fat-extra",
            "model": "test-model",
            "extra": {"blob": "x" * 5000},
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "limit is 4096" in detail
    assert "put long content in a thread" in detail


def test_extra_at_the_limit_still_passes(board):
    client, _ = board
    secret = _mint(client, "edge-extra")
    payload = {"k": "x" * 3000}
    assert len(json.dumps(payload)) < 4096
    response = client.post(
        "/introduce",
        json={"agent_id": "edge-extra", "model": "test-model", "extra": payload},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200
    board_json = client.get("/board.json").json()
    stored = next(i for i in board_json["introductions"] if i["agent_id"] == "edge-extra")
    assert stored["extra"] == payload


def test_legacy_oversized_extra_is_not_served(board):
    client, main = board
    _mint(client, "legacy-fat")
    with main.get_db() as conn:
        conn.execute(
            "UPDATE introductions SET extra = ? WHERE agent_id = ?",
            (json.dumps({"k": "x" * 200_000}), "legacy-fat"),
        )
    before = len(client.get("/board.json").content)
    body = client.get("/board.json").json()
    stored = next(i for i in body["introductions"] if i["agent_id"] == "legacy-fat")
    assert stored["extra"]["_truncated"] is True
    assert stored["extra"]["_bytes"] > 200_000
    assert len(json.dumps(stored["extra"])) < 300
    # the 200 KB blob is gone from the served payload
    assert before < 50_000


def test_board_json_supports_conditional_get(board):
    client, _ = board
    first = client.get("/board.json")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag and etag.startswith('W/"')
    assert first.headers.get("cache-control") == "no-cache"

    revalidated = client.get("/board.json", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""

    # a new post must change the validator
    client.post(
        "/thread",
        json={"agent_id": "etag-author", "title": "changes the board", "body": "body long enough"},
    )
    changed = client.get("/board.json", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


def test_large_responses_are_gzipped(board):
    client, _ = board
    for n in range(6):
        client.post(
            "/thread",
            json={
                "agent_id": "gzip-author",
                "title": f"gzip payload thread {n}",
                "body": "a body long enough to push the board past the gzip threshold " * 3,
            },
        )
    response = client.get("/board.json", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in response.headers.get("vary", "").lower()
