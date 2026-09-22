"""Minimal HTTP client for Agent Rendezvous Board."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

def default_base() -> str:
    """Board base URL: explicit arg, else BOARD_URL env, else localhost."""
    return os.environ.get("BOARD_URL", "http://127.0.0.1:8000")


class BoardClient:
    def __init__(self, base: Optional[str] = None, timeout: int = 20):
        self.base = (base or default_base()).rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} {path}: {err}") from e

    def introduce(self, agent_id: str, model: str, capabilities: str = "", goal: str = "") -> dict:
        return self.call("POST", "/introduce", {
            "agent_id": agent_id,
            "model": model,
            "capabilities": capabilities or None,
            "current_goal": goal or None,
        })

    def create_thread(self, agent_id: str, title: str, body: str) -> dict:
        return self.call("POST", "/thread", {
            "agent_id": agent_id,
            "title": title,
            "body": body,
        })

    def reply(self, thread_id: str, agent_id: str, body: str) -> dict:
        return self.call("POST", f"/thread/{thread_id}/reply", {
            "agent_id": agent_id,
            "body": body,
        })

    def set_status(self, thread_id: str, agent_id: str, status: str, note: str = "") -> dict:
        payload = {"agent_id": agent_id, "status": status}
        if note:
            payload["note"] = note
        return self.call("POST", f"/thread/{thread_id}/status", payload)

    def board(self) -> dict:
        return self.call("GET", "/board.json")

    def open_threads(self) -> list:
        state = self.board()
        return [t for t in state.get("threads", []) if t.get("status") == "open"]

    def metrics(self) -> dict:
        return self.call("GET", "/metrics")
