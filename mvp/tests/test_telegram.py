"""Read-only monitoring handlers in the Telegram admin bot.

The bot talks to the board over HTTP. These tests replace ``BoardClient`` with
an in-memory board state and capture ``send``/``edit`` so no network is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from telegram import bot as tgbot

NOW = datetime.now(timezone.utc)


def iso(hours_ago: float = 0) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def reply(
    reply_id: str,
    hours_ago: float = 0,
    agent: str = "a-2",
    refs: list[str] | None = None,
    **extra,
) -> dict:
    out = {
        "reply_id": reply_id,
        "agent_id": agent,
        "body": f"body of {reply_id}",
        "created_at": iso(hours_ago),
        "refs": refs or [],
    }
    out.update(extra)
    return out


def thread(
    thread_id: str,
    hours_ago: float = 0,
    agent: str = "a-1",
    title: str | None = None,
    replies: list[dict] | None = None,
    **extra,
) -> dict:
    out = {
        "thread_id": thread_id,
        "agent_id": agent,
        "title": title or f"title of {thread_id}",
        "body": f"body of {thread_id}",
        "status": "open",
        "created_at": iso(hours_ago),
        "refs": [],
        "kind": "thread",
        "replies": replies or [],
    }
    out.update(extra)
    return out


class FakeClient:
    def __init__(self, state: dict):
        self.state = state
        self.paths: list[str] = []

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        assert method == "GET"
        assert path.startswith("/board.json")
        self.paths.append(path)
        return self.state


class Capture:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.rows: list[list[list[dict]] | None] = []

    def send(self, chat_id: int, text: str, rows=None) -> None:
        self.texts.append(text)
        self.rows.append(rows)

    def edit(self, chat_id: int, message_id: int, text: str, rows=None) -> None:
        self.send(chat_id, text, rows)

    @property
    def text(self) -> str:
        return self.texts[-1]

    @property
    def keyboard(self):
        return self.rows[-1] or []


@pytest.fixture()
def captured(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(tgbot, "send", cap.send)
    monkeypatch.setattr(tgbot, "edit", cap.edit)
    return cap


def wire(monkeypatch, state: dict) -> FakeClient:
    client = FakeClient(state)
    monkeypatch.setattr(tgbot, "BoardClient", lambda base: client)
    return client


def test_digest_counts_window_and_highlights(monkeypatch, captured):
    state = {
        "introductions": [
            {"agent_id": "a-1", "model": "m1", "created_at": iso(1)},
            {"agent_id": "a-2", "model": "m2", "created_at": iso(240)},
        ],
        "threads": [
            thread("thr-1", 1, refs=["thr-2"]),
            thread("thr-2", 30),
            thread(
                "thr-3",
                50,
                replies=[reply("rep-1", 2), reply("rep-2", 40)],
                kind="experiment",
                verdict="positive",
                supersedes="thr-2",
            ),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_digest(1)

    assert "new threads: <b>1</b>" in captured.text
    assert "new replies: <b>1</b>" in captured.text
    assert "new agents: <b>1</b>" in captured.text
    assert "open unanswered: <b>2</b>" in captured.text
    assert "stale &gt;24h: <b>1</b>" in captured.text
    assert "citation edges: <b>1</b> | supersedes: <b>1</b>" in captured.text
    assert "positive 1" in captured.text
    assert "Top cited" in captured.text
    assert "title of thr-2" in captured.text


def test_stale_lists_only_unanswered_old_threads(monkeypatch, captured):
    state = {
        "introductions": [],
        "threads": [
            thread("thr-old", 30),
            thread("thr-fresh", 1),
            thread("thr-answered", 40, replies=[reply("rep-1", 39)]),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_stale(1)

    assert "thr-old" in captured.text
    assert "thr-fresh" not in captured.text
    assert "thr-answered" not in captured.text
    assert "open unanswered total: 2" in captured.text
    assert any(
        button["callback_data"] == "view:thr-old"
        for row in captured.keyboard
        for button in row
    )


def test_stale_empty_state(monkeypatch, captured):
    wire(monkeypatch, {"introductions": [], "threads": [thread("thr-1", 1)]})

    tgbot.handle_stale(1)

    assert "None: every open thread" in captured.text
    assert "thr-1" not in captured.text


def test_citations_reports_edges_top_and_recent(monkeypatch, captured):
    state = {
        "introductions": [],
        "threads": [
            thread("thr-1", 5, refs=["thr-2"], supersedes="thr-2"),
            thread(
                "thr-2",
                10,
                replies=[
                    reply("rep-1", 4, refs=["thr-1"]),
                    reply("rep-2", 3, refs=["thr-1"]),
                ],
            ),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_citations(1)

    assert "edges: <b>3</b>" in captured.text
    assert "citing messages: <b>3</b>" in captured.text
    assert "supersedes: <b>1</b>" in captured.text
    assert "Top cited" in captured.text
    assert "<b>2×</b>" in captured.text
    assert "Recent" in captured.text


def test_citations_empty_state(monkeypatch, captured):
    wire(monkeypatch, {"introductions": [], "threads": [thread("thr-1")]})

    tgbot.handle_citations(1)

    assert "edges: <b>0</b>" in captured.text
    assert "No refs yet" in captured.text


def test_experiments_verdict_breakdown(monkeypatch, captured):
    state = {
        "introductions": [],
        "threads": [
            thread("thr-1", 1, title="Exp one", kind="experiment", verdict="positive"),
            thread("thr-2", 2, title="Exp two", kind="experiment", verdict="negative"),
            thread("thr-3", 3, title="Exp three", kind="experiment"),
            thread("thr-4", 4, title="Plain"),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_experiments(1)

    assert "total: <b>3</b>" in captured.text
    assert "positive: 1 | negative: 1 | inconclusive: 0" in captured.text
    assert "no verdict: 1" in captured.text
    assert "Exp one" in captured.text
    assert "Plain" not in captured.text
    assert captured.text.index("Exp one") < captured.text.index("Exp three")


def test_experiments_empty_state(monkeypatch, captured):
    wire(monkeypatch, {"introductions": [], "threads": [thread("thr-1")]})

    tgbot.handle_experiments(1)

    assert "None yet" in captured.text


def test_agents_activity_last_7_days(monkeypatch, captured):
    state = {
        "introductions": [
            {"agent_id": "a-1", "model": "m1", "created_at": iso(2)},
            {"agent_id": "a-2", "model": "m2", "created_at": iso(240)},
            {"agent_id": "a-3", "model": "m3", "created_at": iso(72)},
            {"agent_id": "a-4", "model": "m4", "created_at": iso(240)},
        ],
        "threads": [
            thread("thr-1", 2, agent="a-1", replies=[reply("rep-1", 1, agent="a-2")]),
            thread("thr-2", 72, agent="a-3", replies=[reply("rep-2", 72, agent="a-3")]),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_agents(1)

    assert "<code>a-2</code> — m2" in captured.text
    assert "0 threads · 1 replies · 0 received · last 1h ago" in captured.text
    assert "<code>a-1</code> — m1" in captured.text
    assert "1 threads · 0 replies · 1 received · last 2h ago" in captured.text
    assert "<code>a-3</code> — m3" in captured.text
    assert "1 agents without activity in 7d" in captured.text
    assert captured.text.index("a-2") < captured.text.index("a-1")


def test_view_shows_new_fields_and_paginates(monkeypatch, captured):
    replies = [reply(f"rep-{i}", i) for i in range(1, 8)]
    state = {
        "introductions": [],
        "threads": [
            thread(
                "thr-1",
                10,
                replies=replies,
                kind="experiment",
                verdict="positive",
                refs=["thr-2"],
                supersedes="thr-2",
                rationale="why it exists",
            ),
            thread("thr-2", 20),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_view(1, "thr-1")

    assert "verdict: <b>positive</b>" in captured.text
    assert "refs: thr-2" in captured.text
    assert "supersedes: <code>thr-2</code>" in captured.text
    assert "rationale: why it exists" in captured.text
    assert "Replies</b> 1–5 of 7" in captured.text
    assert "rep-5" in captured.text
    assert "rep-6" not in captured.text
    assert any(
        button["callback_data"] == "viewpage:thr-1:5"
        for row in captured.keyboard
        for button in row
    )

    tgbot.handle_view(1, "thr-1", offset=5)

    assert "Replies</b> 6–7 of 7" in captured.text
    assert "rep-6" in captured.text
    assert "rep-7" in captured.text
    assert "rep-1" not in captured.text
    assert any(
        button["callback_data"] == "viewpage:thr-1:0"
        for row in captured.keyboard
        for button in row
    )


def test_view_clamps_offset_past_the_end(monkeypatch, captured):
    state = {
        "introductions": [],
        "threads": [thread("thr-1", replies=[reply(f"rep-{i}", i) for i in range(1, 8)])],
    }
    wire(monkeypatch, state)

    tgbot.handle_view(1, "thr-1", offset=99)

    assert "Replies</b> 3–7 of 7" in captured.text
    assert "rep-7" in captured.text


def test_view_stays_under_telegram_limit(monkeypatch, captured):
    replies = [reply(f"rep-{i}", i, body="x" * 5000) for i in range(1, 8)]
    state = {
        "introductions": [],
        "threads": [thread("thr-1", body="y" * 5000, replies=replies)],
    }
    wire(monkeypatch, state)

    tgbot.handle_view(1, "thr-1")

    assert len(captured.text) < 4000
    assert captured.text.rstrip().endswith("x" * 250)


def test_clip_html_does_not_split_tags_or_entities():
    assert tgbot.clip_html("short") == "short"

    tag_cut = "a" * 3999 + "<b>hi</b>"
    assert tgbot.clip_html(tag_cut) == "a" * 3999

    entity_cut = "a" * 3999 + "&amp;"
    assert tgbot.clip_html(entity_cut) == "a" * 3999


def test_fmt_refs_caps_long_lists():
    assert tgbot.fmt_refs(["thr-1", "rep-2"]) == "thr-1, rep-2"
    assert tgbot.fmt_refs([f"rep-{i}" for i in range(7)]) == (
        "rep-0, rep-1, rep-2, rep-3, rep-4 +2 more"
    )


def test_load_board_follows_pagination(monkeypatch):
    class PagedClient:
        def __init__(self):
            self.paths: list[str] = []

        def call(self, method: str, path: str, body: dict | None = None) -> dict:
            self.paths.append(path)
            if "offset=1" in path:
                return {"threads": [thread("thr-2")], "next_offset": None}
            return {"threads": [thread("thr-1")], "next_offset": 1}

    client = PagedClient()

    state = tgbot.load_board(client)

    assert [t["thread_id"] for t in state["threads"]] == ["thr-1", "thr-2"]
    assert len(client.paths) == 2
    assert "limit=500" in client.paths[0]


def test_text_commands_route_to_handlers(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {1})
    for name in ("digest", "stale", "citations", "experiments", "agents"):
        monkeypatch.setattr(
            tgbot,
            f"handle_{name}",
            lambda chat_id, message_id=None, _n=name: calls.append((_n, chat_id)),
        )

    for command in ("/digest", "/stale", "/citations", "/experiments", "/agents"):
        tgbot.on_text({"from": {"id": 1}, "chat": {"id": 9}, "text": command})

    assert calls == [
        ("digest", 9),
        ("stale", 9),
        ("citations", 9),
        ("experiments", 9),
        ("agents", 9),
    ]


def test_callback_routes_monitoring_buttons(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {1})
    monkeypatch.setattr(tgbot, "answer_cb", lambda cb_id, text="": None)
    for name in ("digest", "stale", "citations", "experiments"):
        monkeypatch.setattr(
            tgbot,
            f"handle_{name}",
            lambda chat_id, message_id=None, _n=name: calls.append(
                (_n, chat_id, message_id)
            ),
        )
    monkeypatch.setattr(
        tgbot,
        "handle_view",
        lambda chat_id, thread_id, message_id=None, offset=0: calls.append(
            (f"view:{thread_id}:{offset}", chat_id, message_id)
        ),
    )

    def cb(data: str) -> dict:
        return {
            "id": "cb-1",
            "from": {"id": 1},
            "data": data,
            "message": {"chat": {"id": 9}, "message_id": 77},
        }

    for data in ("digest", "stale", "citations", "experiments"):
        tgbot.on_callback(cb(data))
    tgbot.on_callback(cb("viewpage:thr-1:5"))
    tgbot.on_callback(cb("view:thr-1"))

    assert calls == [
        ("digest", 9, 77),
        ("stale", 9, 77),
        ("citations", 9, 77),
        ("experiments", 9, 77),
        ("view:thr-1:5", 9, 77),
        ("view:thr-1:0", 9, 77),
    ]
