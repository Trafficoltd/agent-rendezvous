"""Shared pytest fixtures for the board API tests."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def _make_board(tmp_path, monkeypatch, admin_secret=None):
    """Reload the app against an isolated SQLite file and start it.

    Reloading resets the in-memory rate limiter and re-runs ``init_db``
    (via the app lifespan) against the temporary database for each test.
    """
    monkeypatch.setenv("BOARD_DB_PATH", str(tmp_path / "board.db"))
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    if admin_secret is None:
        monkeypatch.delenv("ADMIN_SECRET", raising=False)
    else:
        monkeypatch.setenv("ADMIN_SECRET", admin_secret)

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


@pytest.fixture()
def board(tmp_path, monkeypatch):
    yield from _make_board(tmp_path, monkeypatch)


@pytest.fixture()
def board_with_admin(tmp_path, monkeypatch):
    yield from _make_board(tmp_path, monkeypatch, admin_secret="s3cret")
