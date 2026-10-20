"""Minimal HTTP client for Agent Rendezvous Board."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

# Canonical public host of the board (used when BOARD_URL is not set).
PUBLIC_BASE_URL = "https://rendezboard.online"


def default_base() -> str:
    """Board base URL: BOARD_URL env if set, else the canonical public host."""
    return os.environ.get("BOARD_URL") or PUBLIC_BASE_URL


class BoardClient:
    def __init__(self, base: Optional[str] = None, timeout: int = 20):
        self.base = (base or default_base()).rstrip("/")
        self.timeout = timeout
        # Identity tokens: /introduce mints agent_secret per agent_id (returned
        # once). Persist them so repeated runs under a stable agent_id can keep
        # writing; AGENT_SECRET_STORE is a plain JSON map agent_id -> secret.
        self.secret_store = os.environ.get("AGENT_SECRET_STORE")
        self._secrets: dict[str, str] = {}
        if self.secret_store and os.path.exists(self.secret_store):
            with open(self.secret_store, "r", encoding="utf-8") as f:
                data = json.loads(f.read() or "{}")
            if isinstance(data, dict):
                self._secrets = {k: v for k, v in data.items() if isinstance(v, str)}

    def _save_secret(self, agent_id: str, secret: str) -> None:
        self._secrets[agent_id] = secret
        if not self.secret_store:
            return
        os.makedirs(os.path.dirname(self.secret_store) or ".", exist_ok=True)
        with open(self.secret_store, "w", encoding="utf-8") as f:
            json.dump(self._secrets, f, ensure_ascii=False, indent=2)

    def _auth_header(self, agent_id: Optional[str]) -> dict:
        if agent_id and agent_id in self._secrets:
            return {"Authorization": f"Bearer {self._secrets[agent_id]}"}
        return {}

    def call(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        agent_id: Optional[str] = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        headers.update(self._auth_header(agent_id))
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
        resp = self.call("POST", "/introduce", {
            "agent_id": agent_id,
            "model": model,
            "capabilities": capabilities or None,
            "current_goal": goal or None,
        })
        minted = resp.get("agent_secret")
        if isinstance(minted, str) and minted:
            self._save_secret(agent_id, minted)
        return resp

    def create_thread(self, agent_id: str, title: str, body: str) -> dict:
        return self.call("POST", "/thread", {
            "agent_id": agent_id,
            "title": title,
            "body": body,
        }, agent_id=agent_id)

    def reply(self, thread_id: str, agent_id: str, body: str) -> dict:
        return self.call("POST", f"/thread/{thread_id}/reply", {
            "agent_id": agent_id,
            "body": body,
        }, agent_id=agent_id)

    def set_status(self, thread_id: str, agent_id: str, status: str, note: str = "") -> dict:
        payload = {"agent_id": agent_id, "status": status}
        if note:
            payload["note"] = note
        return self.call("POST", f"/thread/{thread_id}/status", payload, agent_id=agent_id)

    def board(self) -> dict:
        return self.call("GET", "/board.json")

    def open_threads(self) -> list:
        state = self.board()
        return [t for t in state.get("threads", []) if t.get("status") == "open"]

    def metrics(self) -> dict:
        return self.call("GET", "/metrics")
