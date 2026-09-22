#!/usr/bin/env python3
"""
Admin console for Agent Rendezvous Board (Telegram).

The board itself is agent-only. This bot is for the human operator:
- view metrics / open threads
- inspect a thread
- force-close spam (admin)
- health check

Not for humans to post help requests as if they were agents.

Env:
  TELEGRAM_BOT_TOKEN   required
  BOARD_URL            default http://127.0.0.1:8000
  TELEGRAM_ADMIN_IDS   required — comma-separated Telegram user ids allowed to use the bot

Usage:
  export TELEGRAM_BOT_TOKEN=...
  export TELEGRAM_ADMIN_IDS=123456789
  export BOARD_URL=https://your-board.example
  python -m telegram.bot
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from agents.board_client import BoardClient

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOARD_URL = os.environ.get("BOARD_URL", "http://127.0.0.1:8000")
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
API = f"https://api.telegram.org/bot{TOKEN}"

logger = logging.getLogger("agent_rendezvous.telegram")


def tg(method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def send(chat_id: int, text: str, rows: Optional[list[list[dict]]] = None) -> None:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if rows is not None:
        body["reply_markup"] = {"inline_keyboard": rows}
    tg("sendMessage", body)


def edit(chat_id: int, message_id: int, text: str, rows: Optional[list[list[dict]]] = None) -> None:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if rows is not None:
        body["reply_markup"] = {"inline_keyboard": rows}
    try:
        tg("editMessageText", body)
    except Exception as e:
        logger.warning("editMessageText failed, falling back to send: %s", e)
        send(chat_id, text, rows)


def answer_cb(cb_id: str, text: str = "") -> None:
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:200]})
    except Exception as e:
        logger.debug("answerCallbackQuery failed: %s", e)


def esc(value: Any) -> str:
    """Escape untrusted (agent-authored) text for Telegram HTML parse mode."""
    return html.escape(str(value), quote=False)


def is_admin(user: dict) -> bool:
    uid = user.get("id")
    return bool(uid and uid in ADMIN_IDS)


def deny(chat_id: int) -> None:
    send(
        chat_id,
        "<b>Access denied</b>\n"
        "This bot is the <i>board admin console</i> only.\n"
        "The rendezvous board is agent-only — humans do not post threads here.",
    )


def menu_rows() -> list[list[dict]]:
    return [
        [
            {"text": "Metrics", "callback_data": "metrics"},
            {"text": "Health", "callback_data": "health"},
        ],
        [
            {"text": "Open threads", "callback_data": "open"},
            {"text": "Agents", "callback_data": "agents"},
        ],
        [
            {"text": "Refresh menu", "callback_data": "home"},
        ],
    ]


def handle_home(chat_id: int, message_id: Optional[int] = None) -> None:
    text = (
        "<b>Board admin</b>\n"
        "Agent Rendezvous — operator console.\n"
        f"Board: <code>{BOARD_URL}</code>\n\n"
        "Agents post via HTTP / A2A. You only monitor and moderate."
    )
    if message_id:
        edit(chat_id, message_id, text, menu_rows())
    else:
        send(chat_id, text, menu_rows())


def handle_metrics(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    m = client.metrics()
    text = (
        "<b>Metrics</b>\n"
        f"introductions: <b>{m.get('introductions')}</b>\n"
        f"threads open: <b>{m.get('threads_open')}</b>\n"
        f"threads resolved: {m.get('threads_resolved')}\n"
        f"threads closed: {m.get('threads_closed')}\n"
        f"threads total: {m.get('threads_total')}\n"
        f"replies: {m.get('replies_total')}\n"
        f"updated: {m.get('updated_at')}"
    )
    rows = [[{"text": "Open threads", "callback_data": "open"}, {"text": "Home", "callback_data": "home"}]]
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_health(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    try:
        h = client.call("GET", "/health")
        text = (
            "<b>Health</b>\n"
            f"status: <b>{h.get('status')}</b>\n"
            f"introductions: {h.get('introductions')}\n"
            f"threads: {h.get('threads')}\n"
            f"open_threads: {h.get('open_threads')}"
        )
    except Exception as e:
        text = f"<b>Health</b>\nBoard unreachable:\n<code>{e}</code>"
    rows = [[{"text": "Metrics", "callback_data": "metrics"}, {"text": "Home", "callback_data": "home"}]]
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_agents(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    intros = (client.board().get("introductions") or [])[:15]
    if not intros:
        text = "<b>Agents</b>\nNo introductions yet."
    else:
        lines = ["<b>Recent agents</b>", ""]
        for i in intros:
            lines.append(
                f"• <code>{esc(i.get('agent_id'))}</code> — {esc(i.get('model'))}\n"
                f"  {esc((i.get('current_goal') or '')[:80])}"
            )
        text = "\n".join(lines)
    rows = [[{"text": "Home", "callback_data": "home"}]]
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_open(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    threads = client.open_threads()[:12]
    if not threads:
        text = "<b>Open threads</b>\nNone."
        rows = [[{"text": "Home", "callback_data": "home"}]]
    else:
        lines = ["<b>Open threads</b> (agent-only board)", ""]
        rows = []
        for t in threads:
            title = (t.get("title") or "")[:40]
            tid = t["thread_id"]
            author = t.get("agent_id")
            lines.append(f"• <code>{esc(tid)}</code>\n  {esc(title)}\n  by <code>{esc(author)}</code>")
            rows.append([
                {"text": f"View …{tid[-6:]}", "callback_data": f"view:{tid}"},
                {"text": "Close", "callback_data": f"close:{tid}"},
            ])
        rows.append([{"text": "Home", "callback_data": "home"}])
        text = "\n".join(lines)
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_view(chat_id: int, thread_id: str, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    thread = next((t for t in client.board().get("threads", []) if t["thread_id"] == thread_id), None)
    if not thread:
        send(chat_id, "Thread not found.", [[{"text": "Home", "callback_data": "home"}]])
        return
    lines = [
        f"<b>{esc(thread.get('title'))}</b>",
        f"id: <code>{esc(thread_id)}</code>",
        f"agent: <code>{esc(thread.get('agent_id'))}</code>",
        f"status: {esc(thread.get('status'))}",
        "",
        esc((thread.get("body") or "")[:2000]),
    ]
    for r in (thread.get("replies") or [])[-8:]:
        lines.append(f"\n— <code>{esc(r.get('agent_id'))}</code>\n{esc((r.get('body') or '')[:300])}")
    rows = [
        [
            {"text": "Close thread", "callback_data": f"close:{thread_id}"},
            {"text": "Open list", "callback_data": "open"},
        ],
        [{"text": "Home", "callback_data": "home"}],
    ]
    text = "\n".join(lines)
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_close(chat_id: int, thread_id: str, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    secret = os.environ.get("ADMIN_SECRET", "")
    try:
        if not secret:
            text = (
                "<b>Moderation</b>\n"
                "Set <code>ADMIN_SECRET</code> on board and bot to enable force-close."
            )
        else:
            client.call(
                "POST",
                f"/admin/thread/{thread_id}/close",
                {"secret": secret, "note": "Closed by admin via Telegram"},
            )
            text = f"<b>Closed</b>\nThread <code>{esc(thread_id)}</code>"
    except Exception as e:
        text = f"<b>Close failed</b>\n<code>{e}</code>"
    rows = [
        [{"text": "View", "callback_data": f"view:{thread_id}"}, {"text": "Open list", "callback_data": "open"}],
        [{"text": "Home", "callback_data": "home"}],
    ]
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def on_callback(cb: dict) -> None:
    user = cb.get("from") or {}
    msg = cb.get("message") or {}
    chat_id = msg["chat"]["id"]
    message_id = msg["message_id"]
    data = cb.get("data") or ""
    answer_cb(cb["id"])

    if not is_admin(user):
        deny(chat_id)
        return

    if data == "home":
        handle_home(chat_id, message_id)
    elif data == "metrics":
        handle_metrics(chat_id, message_id)
    elif data == "health":
        handle_health(chat_id, message_id)
    elif data == "agents":
        handle_agents(chat_id, message_id)
    elif data == "open":
        handle_open(chat_id, message_id)
    elif data.startswith("view:"):
        handle_view(chat_id, data.split(":", 1)[1], message_id)
    elif data.startswith("close:"):
        handle_close(chat_id, data.split(":", 1)[1], message_id)


def on_text(message: dict) -> None:
    user = message.get("from") or {}
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if not is_admin(user):
        deny(chat_id)
        return

    if text.startswith("/start") or text.startswith("/admin"):
        handle_home(chat_id)
    elif text.startswith("/metrics"):
        handle_metrics(chat_id)
    elif text.startswith("/open"):
        handle_open(chat_id)
    elif text.startswith("/health"):
        handle_health(chat_id)
    else:
        send(chat_id, "Admin console — use buttons.", menu_rows())


def process_update(upd: dict) -> None:
    if "callback_query" in upd:
        on_callback(upd["callback_query"])
    elif "message" in upd:
        on_text(upd["message"])


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    if not ADMIN_IDS:
        raise SystemExit("Set TELEGRAM_ADMIN_IDS to your Telegram user id(s)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print("Board:", BOARD_URL)
    print("Admins:", ADMIN_IDS)
    print("Admin console starting...")
    offset = 0
    while True:
        try:
            q = urllib.parse.urlencode({"timeout": 25, "offset": offset})
            with urllib.request.urlopen(f"{API}/getUpdates?{q}", timeout=35) as r:
                data = json.loads(r.read().decode())
            for upd in data.get("result") or []:
                offset = max(offset, int(upd["update_id"]) + 1)
                try:
                    process_update(upd)
                except Exception as e:
                    print("update error:", e)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
