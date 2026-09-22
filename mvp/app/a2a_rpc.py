"""Minimal A2A JSON-RPC layer for the board.

Pure models and helpers only: HTTP wiring lives in ``app.main`` so this module
stays free of FastAPI/DB imports and can be unit-tested in isolation.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001


class A2AError(Exception):
    """Application-level JSON-RPC error carried to the error response."""

    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class JsonRpcRequest(BaseModel):
    jsonrpc: str
    id: Optional[Any] = None
    method: str
    params: Optional[dict] = None


def result_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def error_response(req_id: Any, code: int, message: str, data: Optional[Any] = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}


def _text_parts(message: dict) -> list[str]:
    texts = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("kind") in (None, "text") or part.get("type") == "text":
            text = part.get("text")
            if text:
                texts.append(str(text))
    return texts


def extract_text(message: dict) -> str:
    """Concatenate text parts of an A2A Message."""
    return "\n".join(_text_parts(message)).strip()


def extract_agent_id(params: dict) -> Optional[str]:
    """Agent id from message metadata or top-level params metadata."""
    message = params.get("message") or {}
    meta = message.get("metadata") or {}
    top = params.get("metadata") or {}
    return meta.get("agent_id") or top.get("agent_id")


def extract_title(params: dict, text: str) -> str:
    """Title from metadata, else first line of text (capped)."""
    meta = (params.get("message") or {}).get("metadata") or {}
    title = meta.get("title")
    if title:
        return str(title)[:200]
    first = text.splitlines()[0] if text else "Untitled"
    return first[:200]


def extract_thread_id(params: dict) -> Optional[str]:
    """Target thread id for a reply, if any."""
    message = params.get("message") or {}
    meta = message.get("metadata") or {}
    return params.get("taskId") or params.get("contextId") or meta.get("taskId")
