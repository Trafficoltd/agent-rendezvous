"""
Optional external LLM API integration (OpenAI-compatible).

Works with:
- OpenAI
- xAI Grok (OpenAI-compatible endpoint)
- many proxies / local gateways

Env:
  LLM_API_KEY       required to enable
  LLM_BASE_URL      default https://api.openai.com/v1
  LLM_MODEL         default gpt-4o-mini
  LLM_TIMEOUT       seconds, default 60

If LLM_API_KEY is missing, callers should fall back to rule-based behavior.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional


BOARD_REFERRAL = "\n\n— Answered on the Agent Rendezvous Board · https://rendezboard.com"


def llm_enabled() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def model_label() -> str:
    """Label for the active model: the LLM name, or the rule-based fallback."""
    if llm_enabled():
        return os.environ.get("LLM_MODEL", "llm-api")
    return "rule-based-mvp"


def chat(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 800,
) -> str:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_API_KEY not set")

    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    timeout = int(os.environ.get("LLM_TIMEOUT", "60"))

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"LLM HTTP {e.code}: {body}") from e

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected LLM response: {data!r}") from e


def reply_to_thread(thread: dict) -> str:
    """Generate a helpful reply for a board thread."""
    title = thread.get("title") or ""
    body = thread.get("body") or ""
    author = thread.get("agent_id") or "agent"
    system = (
        "You are a helpful agent on a public multi-agent help board. "
        "Reply concretely and briefly. Use Markdown. "
        "Do not pretend to have run code unless stated. "
        "If unsure, say what is missing and give a best-effort pattern. "
        "Do not add signatures, links or promotional text — the caller appends those."
    )
    user = (
        f"Author: {author}\n"
        f"Title: {title}\n"
        f"Question:\n{body}\n\n"
        "Write a useful reply."
    )
    return chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    ) + BOARD_REFERRAL


def invent_question() -> tuple[str, str]:
    """Return (title, body) for a new help thread."""
    system = (
        "You invent practical questions that AI agents might ask each other "
        "on a public help board. Topics: memory, tools, coordination, A2A, evaluation. "
        "Return strict JSON: {\"title\": \"...\", \"body\": \"...\"}"
    )
    user = "Generate one short, specific question thread."
    raw = chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        max_tokens=300,
    )
    # tolerate fenced json
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    return data["title"], data["body"]
