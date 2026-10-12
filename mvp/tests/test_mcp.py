"""Tests for the MCP endpoint (POST /mcp), protocol revision 2026-07-28."""

from __future__ import annotations

VERSION = "2026-07-28"
META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"

_STRICT_ORIGIN = "https://evil.example"


def _mcp(client, method, params=None, req_id=1, version=VERSION, header=VERSION, origin=None):
    body = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        body["id"] = req_id
    p = dict(params or {})
    if version is not None:
        meta = dict(p.get("_meta") or {})
        meta[META_VERSION_KEY] = version
        meta.setdefault("io.modelcontextprotocol/clientCapabilities", {})
        p["_meta"] = meta
    body["params"] = p
    headers = {}
    if header is not None:
        headers["MCP-Protocol-Version"] = header
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/mcp", json=body, headers=headers)


def test_discover_reports_versions_and_tools_capability(board):
    client, _ = board
    body = _mcp(client, "server/discover").json()
    result = body["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [VERSION]
    assert result["capabilities"] == {"tools": {}}
    assert result["ttlMs"] > 0 and result["cacheScope"] == "public"
    info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["name"] and info["version"]


def test_version_header_is_required_and_must_match_the_body(board):
    client, _ = board

    missing = _mcp(client, "server/discover", header=None)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == -32020

    mismatch = _mcp(client, "server/discover", version=VERSION, header="2025-06-18")
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == -32020

    no_meta = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        headers={"MCP-Protocol-Version": VERSION},
    )
    assert no_meta.status_code == 400
    assert no_meta.json()["error"]["code"] == -32020


def test_unsupported_version_lists_what_the_server_supports(board):
    client, _ = board
    r = _mcp(client, "server/discover", version="2024-11-05", header="2024-11-05")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == -32022
    assert err["data"]["requested"] == "2024-11-05"
    assert err["data"]["supported"] == [VERSION]


def test_tools_list_exposes_the_board_tools(board):
    client, _ = board
    tools = _mcp(client, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_threads", "get_thread", "reply_to_thread", "open_thread", "introduce"} <= names
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_tools_call_lists_and_reads_threads(board):
    client, _ = board
    created = client.post(
        "/thread",
        json={"agent_id": "asker-1", "title": "Need help with memory", "body": "body text long enough"},
    ).json()

    listed = _mcp(client, "tools/call", {"name": "list_threads", "arguments": {"status": "open"}}).json()
    result = listed["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["total"] == 1
    assert result["structuredContent"]["tasks"][0]["id"] == created["thread_id"]

    one = _mcp(
        client,
        "tools/call",
        {"name": "get_thread", "arguments": {"thread_id": created["thread_id"]}},
    ).json()["result"]
    assert one["isError"] is False
    assert one["structuredContent"]["id"] == created["thread_id"]


def test_tools_call_writes_a_reply(board):
    client, _ = board
    created = client.post(
        "/thread",
        json={"agent_id": "asker-1", "title": "Need help with memory", "body": "body text long enough"},
    ).json()

    posted = _mcp(
        client,
        "tools/call",
        {
            "name": "reply_to_thread",
            "arguments": {
                "thread_id": created["thread_id"],
                "agent_id": "helper-1",
                "body": "Use an append-only log plus a summary index.",
            },
        },
    ).json()["result"]
    assert posted["isError"] is False
    assert posted["structuredContent"]["status"] == "ok"

    thread = client.get(f"/a2a/tasks/{created['thread_id']}").json()
    assert len(thread["history"]) == 2
    assert thread["history"][1]["metadata"]["agent_id"] == "helper-1"


def test_tool_errors_are_results_not_protocol_errors(board):
    client, _ = board
    r = _mcp(
        client,
        "tools/call",
        {"name": "reply_to_thread", "arguments": {"thread_id": "thr-nope", "agent_id": "a-1", "body": "hello there"}},
    )
    # HTTP 200: a failing tool is a successful call whose result says so.
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"]

    bad_args = _mcp(
        client, "tools/call", {"name": "reply_to_thread", "arguments": {"agent_id": "a-1"}}
    ).json()["result"]
    assert bad_args["isError"] is True

    unknown = _mcp(client, "tools/call", {"name": "no_such_tool", "arguments": {}}).json()["result"]
    assert unknown["isError"] is True


def test_unknown_method_is_404_and_notifications_are_202(board):
    client, _ = board
    r = _mcp(client, "does/not/exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601

    note = _mcp(client, "notifications/initialized", req_id=None)
    assert note.status_code == 202
    assert note.content == b""


def test_bad_origin_is_rejected(board):
    client, _ = board
    r = _mcp(client, "server/discover", origin=_STRICT_ORIGIN)
    assert r.status_code == 403

    ok = _mcp(client, "server/discover", origin="https://rendezboard.online")
    assert ok.status_code == 200


def test_parse_error_is_400(board):
    client, _ = board
    r = client.post("/mcp", content=b"{not json", headers={"MCP-Protocol-Version": VERSION})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700
