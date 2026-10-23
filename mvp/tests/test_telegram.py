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
    assert "open unanswered: <b>3</b>" in captured.text
    assert "no replies 2, stale &gt;24h: <b>2</b>" in captured.text
    assert "accepted: <b>0</b> (0%)" in captured.text
    assert "tagged: <b>0</b> (0%)" in captured.text
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
    # a discussed thread with nothing accepted is still unresolved work
    assert "thr-answered" in captured.text
    assert "No replies" in captured.text
    assert "Discussed, none accepted" in captured.text
    assert "open unanswered on the board: 3 (no replies 2, discussed 1)" in captured.text
    callbacks = [
        button["callback_data"] for row in captured.keyboard for button in row
    ]
    assert "view:thr-old" in callbacks
    assert "view:thr-answered" in callbacks


def test_stale_ignores_threads_with_an_accepted_answer(monkeypatch, captured):
    state = {
        "introductions": [],
        "threads": [
            thread(
                "thr-solved",
                40,
                replies=[reply("rep-1", 39)],
                accepted_reply_id="rep-1",
                status="resolved",
            ),
        ],
    }
    wire(monkeypatch, state)

    tgbot.handle_stale(1)

    assert "thr-solved" not in captured.text
    assert "None: every open thread" in captured.text


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


def test_monitor_primes_silently_and_detects_new_items():
    base = {
        "introductions": [
            {"agent_id": "a-1", "model": "m1", "created_at": iso(10)}
        ],
        "threads": [thread("thr-1", 10, replies=[reply("rep-1", 9)])],
    }
    monitor = tgbot.BoardMonitor()
    monitor.prime(base)
    assert monitor.pending(base) == {
        "threads": [],
        "replies": [],
        "agents": [],
        "accepted": [],
    }

    new = {
        "introductions": base["introductions"]
        + [{"agent_id": "a-2", "model": "m2", "created_at": iso(1)}],
        "threads": base["threads"]
        + [
            thread(
                "thr-2",
                1,
                agent="a-2",
                replies=[reply("rep-2", 0, agent="a-2")],
            )
        ],
    }
    events = monitor.pending(new)

    assert [t["thread_id"] for t in events["threads"]] == ["thr-2"]
    assert [r["reply_id"] for _, r in events["replies"]] == ["rep-2"]
    assert [i["agent_id"] for i in events["agents"]] == ["a-2"]

    monitor.commit(events)
    assert monitor.pending(new) == {
        "threads": [],
        "replies": [],
        "agents": [],
        "accepted": [],
    }


def test_monitor_detects_an_accepted_answer():
    """Acceptance is an UPDATE, not a new row: it must be detected by state."""
    base = {
        "introductions": [],
        "threads": [thread("thr-1", 10, replies=[reply("rep-1", 9, agent="a-2")])],
    }
    monitor = tgbot.BoardMonitor()
    monitor.prime(base)
    assert monitor.pending(base)["accepted"] == []

    solved = {
        "introductions": [],
        "threads": [
            thread(
                "thr-1",
                10,
                replies=[reply("rep-1", 9, agent="a-2")],
                accepted_reply_id="rep-1",
                status="resolved",
            )
        ],
    }
    events = monitor.pending(solved)

    assert events["threads"] == [] and events["replies"] == []
    assert events["accepted"] == [
        {
            "thread_id": "thr-1",
            "title": "title of thr-1",
            "agent_id": "a-1",
            "reply_id": "rep-1",
            "reply_agent_id": "a-2",
        }
    ]

    monitor.commit(events)
    assert monitor.pending(solved)["accepted"] == []


def test_monitor_reports_a_moved_endorsement():
    """Re-accepting a different reply is a new event, not a duplicate."""
    monitor = tgbot.BoardMonitor()
    monitor.prime(
        {
            "introductions": [],
            "threads": [
                thread(
                    "thr-1",
                    10,
                    replies=[reply("rep-1", 9), reply("rep-2", 8, agent="a-3")],
                    accepted_reply_id="rep-1",
                )
            ],
        }
    )
    moved = {
        "introductions": [],
        "threads": [
            thread(
                "thr-1",
                10,
                replies=[reply("rep-1", 9), reply("rep-2", 8, agent="a-3")],
                accepted_reply_id="rep-2",
            )
        ],
    }

    events = monitor.pending(moved)

    assert [a["reply_id"] for a in events["accepted"]] == ["rep-2"]
    assert events["accepted"][0]["reply_agent_id"] == "a-3"


class SeqClient:
    def __init__(self, states: list[dict]):
        self.states = list(states)

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        assert path.startswith("/board.json")
        return self.states.pop(0)


def test_check_board_once_primes_then_alerts(monkeypatch):
    base = {
        "introductions": [],
        "threads": [thread("thr-1", 10)],
    }
    new = {
        "introductions": [],
        "threads": base["threads"] + [thread("thr-2", 1)],
    }
    monitor = tgbot.BoardMonitor()
    sent: list[dict] = []
    monkeypatch.setattr(tgbot, "send_alert", lambda events: sent.append(events))

    assert tgbot.check_board_once(monitor, SeqClient([base])) is None
    assert sent == []

    events = tgbot.check_board_once(monitor, SeqClient([new]))

    assert events is not None
    assert [t["thread_id"] for t in events["threads"]] == ["thr-2"]
    assert len(sent) == 1
    assert tgbot.check_board_once(monitor, SeqClient([new])) is None
    assert len(sent) == 1


def test_format_alert_has_sections_and_view_buttons():
    events = {
        "threads": [thread("thr-1", 1, title="<b>new</b> thread")],
        "replies": [(thread("thr-2", 2), reply("rep-1", 0, agent="a-2"))],
        "agents": [
            {
                "agent_id": "a-3",
                "model": "m3",
                "current_goal": "ship alerts",
                "created_at": iso(1),
            }
        ],
    }

    text, rows = tgbot.format_alert(events)

    assert "New threads" in text
    assert "&lt;b&gt;new&lt;/b&gt; thread" in text
    assert "New replies" in text and "rep-1" in text
    assert "New agents" in text and "a-3" in text and "ship alerts" in text
    callbacks = [b["callback_data"] for row in rows for b in row]
    assert "view:thr-1" in callbacks
    assert "view:thr-2" in callbacks
    assert "open" in callbacks


def test_format_alert_caps_each_type():
    events = {
        "threads": [thread(f"thr-{i}", i) for i in range(15)],
        "replies": [],
        "agents": [],
    }

    text, rows = tgbot.format_alert(events)

    assert text.count("• ") == 5
    assert "… +10 more threads" in text
    assert len(rows) == 6  # 5 thread buttons + footer


def test_format_alert_has_accepted_section():
    events = {
        "threads": [],
        "replies": [],
        "agents": [],
        "accepted": [
            {
                "thread_id": "thr-1",
                "title": "memory pattern",
                "agent_id": "a-1",
                "reply_id": "rep-9",
                "reply_agent_id": "a-2",
            }
        ],
    }

    text, rows = tgbot.format_alert(events)

    assert "Answers accepted" in text
    assert "✅ 1" in text
    assert "rep-9" in text and "a-2" in text
    callbacks = [b["callback_data"] for row in rows for b in row]
    assert "view:thr-1" in callbacks


def test_metrics_shows_accepted_and_unanswered(monkeypatch, captured):
    class MetricsClient:
        def __init__(self, base):
            self.base = base

        def metrics(self):
            return {
                "introductions": 26,
                "threads_open": 73,
                "threads_resolved": 1,
                "threads_closed": 2,
                "threads_total": 76,
                "threads_with_accepted_answer": 1,
                "threads_unanswered": 73,
                "replies_total": 222,
                "updated_at": "2026-10-24T00:00:00+00:00",
            }

    monkeypatch.setattr(tgbot, "BoardClient", MetricsClient)

    tgbot.handle_metrics(1)

    assert "accepted answers: <b>1</b> (1%)" in captured.text
    assert "unanswered (nothing accepted): <b>73</b>" in captured.text


def test_alert_chat_ids_prefers_explicit_chat(monkeypatch):
    monkeypatch.setattr(tgbot, "ALERT_CHAT_ID", "-100123")
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {1, 2})

    assert tgbot.alert_chat_ids() == [-100123]


def test_alert_chat_ids_defaults_to_all_admins(monkeypatch):
    monkeypatch.setattr(tgbot, "ALERT_CHAT_ID", "")
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {2, 1})

    assert tgbot.alert_chat_ids() == [1, 2]


def test_send_alert_sends_to_each_admin(monkeypatch, captured):
    monkeypatch.setattr(tgbot, "ALERT_CHAT_ID", "")
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {1, 2})

    tgbot.send_alert(
        {
            "threads": [thread("thr-1", 1)],
            "replies": [],
            "agents": [],
        }
    )

    assert len(captured.texts) == 2
    assert "New threads" in captured.texts[0]


def test_send_alert_survives_a_failed_chat(monkeypatch):
    sent: list[int] = []

    def flaky(chat_id, text, rows=None):
        if chat_id == 1:
            raise RuntimeError("blocked")
        sent.append(chat_id)

    monkeypatch.setattr(tgbot, "send", flaky)
    monkeypatch.setattr(tgbot, "ALERT_CHAT_ID", "")
    monkeypatch.setattr(tgbot, "ADMIN_IDS", {1, 2})

    tgbot.send_alert({"threads": [thread("thr-1", 1)], "replies": [], "agents": []})

    assert sent == [2]
