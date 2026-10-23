"""Tests for the bounded round runner (agents.round)."""

from __future__ import annotations

from agents import round as board_round


def _thread(tid, *, status="open", replies=None, author="other", created="2026-01-01T00:00:00+00:00"):
    return {
        "thread_id": tid,
        "status": status,
        "replies": replies or [],
        "agent_id": author,
        "created_at": created,
        "title": f"title {tid}",
        "body": "body text long enough",
    }


def test_select_candidate_prefers_the_oldest_unanswered_thread():
    threads = [
        _thread("new", created="2026-05-01T00:00:00+00:00"),
        _thread("old", created="2026-01-01T00:00:00+00:00"),
        _thread("mid", created="2026-03-01T00:00:00+00:00"),
    ]
    assert board_round.select_candidate(threads, "me")["thread_id"] == "old"


def test_select_candidate_skips_answered_own_and_closed():
    threads = [
        _thread("answered", replies=[{"agent_id": "x", "body": "hi"}]),
        _thread("mine", author="me"),
        _thread("closed", status="closed"),
        _thread("ok"),
    ]
    assert board_round.select_candidate(threads, "me")["thread_id"] == "ok"


def test_select_candidate_returns_none_when_nothing_waits():
    assert board_round.select_candidate([_thread("a", replies=[{"agent_id": "x"}])], "me") is None
    assert board_round.select_candidate([], "me") is None


def test_run_once_refuses_to_post_without_an_llm(monkeypatch, capsys):
    """A scheduled job must never fall back to the boilerplate template."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(board_round, "fetch_round_prompt", lambda base, timeout=30: "prompt")

    class FakeClient:
        base = "http://board.invalid"

        def board(self):
            return {"threads": [_thread("waiting")]}

        def reply(self, *args, **kwargs):
            raise AssertionError("must not post without an LLM configured")

    posted = board_round.run_once(FakeClient(), "me", 1, dry_run=False)
    assert posted == 0
    out = capsys.readouterr().out
    # The gate is checked before selecting a candidate: fail fast, do the work only
    # when an answer can actually be produced.
    assert "refusing to post" in out
    assert "board: 1 threads" in out


def test_run_once_dry_run_selects_without_posting(monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(board_round, "fetch_round_prompt", lambda base, timeout=30: "prompt")

    class FakeClient:
        base = "http://board.invalid"

        def board(self):
            return {"threads": [_thread("waiting")]}

        def reply(self, *args, **kwargs):
            raise AssertionError("dry run must not post")

    assert board_round.run_once(FakeClient(), "me", 1, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "refusing to post" not in out


def test_run_once_posts_one_reply_when_llm_is_available(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(board_round, "fetch_round_prompt", lambda base, timeout=30: "prompt")
    monkeypatch.setattr(board_round.llm, "reply_to_thread", lambda thread: "A concrete answer.")

    sent = []

    class FakeClient:
        base = "http://board.invalid"

        def board(self):
            return {"threads": [_thread("waiting")]}

        def reply(self, thread_id, agent_id, body):
            sent.append((thread_id, agent_id, body))
            return {"status": "ok"}

    assert board_round.run_once(FakeClient(), "me", 1, dry_run=False) == 1
    assert sent == [("waiting", "me", "A concrete answer.")]


def test_run_once_stops_when_the_llm_fails(monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(board_round, "fetch_round_prompt", lambda base, timeout=30: "prompt")

    def boom(thread):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(board_round.llm, "reply_to_thread", boom)

    class FakeClient:
        base = "http://board.invalid"

        def board(self):
            return {"threads": [_thread("waiting")]}

        def reply(self, *args, **kwargs):
            raise AssertionError("must not post when generation failed")

    assert board_round.run_once(FakeClient(), "me", 1, dry_run=False) == 0
    assert "LLM failed, nothing posted" in capsys.readouterr().out
