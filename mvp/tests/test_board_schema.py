"""Guards /board.json against the published JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "schemas" / "board.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def _validate(client):
    error = next(VALIDATOR.iter_errors(client.get("/board.json").json()), None)
    assert error is None, error


def test_board_json_matches_schema_full(board):
    client, _ = board
    client.post(
        "/introduce",
        json={"agent_id": "a-1", "model": "m", "capabilities": "c", "current_goal": "g"},
    )
    tid = client.post(
        "/thread", json={"agent_id": "a-1", "title": "valid title", "body": "valid body text"}
    ).json()["thread_id"]
    client.post(f"/thread/{tid}/reply", json={"agent_id": "a-2", "body": "answer"})
    _validate(client)


def test_board_json_matches_schema_minimal_intro(board):
    client, _ = board
    client.post("/introduce", json={"agent_id": "a-1", "model": "m"})
    _validate(client)
