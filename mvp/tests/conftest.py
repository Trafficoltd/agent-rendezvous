"""Shared pytest fixtures for the board API tests."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A fresh board app backed by an isolated SQLite file.

    Reloading the module resets the in-memory rate limiter and re-runs
    ``init_db`` against the temporary database for each test.
    """
    monkeypatch.setenv("BOARD_DB_PATH", str(tmp_path / "board.db"))
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main
