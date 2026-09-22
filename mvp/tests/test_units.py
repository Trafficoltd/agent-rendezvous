"""Unit tests for pure helpers (no network / no board)."""

from __future__ import annotations

from telegram.bot import esc


def test_esc_neutralizes_html():
    assert esc("<b>hi</b> & 'x'") == "&lt;b&gt;hi&lt;/b&gt; &amp; 'x'"


def test_esc_handles_non_string():
    assert esc(42) == "42"
    assert esc(None) == "None"


def test_model_label_defaults_to_rule_based(monkeypatch):
    from agents import llm

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert llm.model_label() == "rule-based-mvp"


def test_model_label_uses_env_model(monkeypatch):
    from agents import llm

    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    assert llm.model_label() == "my-model"
