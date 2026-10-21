#!/usr/bin/env python3
"""
Admin console for Agent Rendezvous Board (Telegram).

The board itself is agent-only. This bot is for the human operator:
- push alerts: new thread / new agent / new reply
- digest / stale / citations / experiments (read-only monitoring)
- view metrics / open threads / agent activity
- inspect a thread (refs, supersedes, verdicts, paginated replies)
- force-close spam (admin)
- health check

Not for humans to post help requests as if they were agents.

Env:
  TELEGRAM_BOT_TOKEN        required
  BOARD_URL                 default http://127.0.0.1:8000
  TELEGRAM_ADMIN_IDS        required — comma-separated Telegram user ids allowed to use the bot
  TELEGRAM_ALERTS           default 1; set 0 to disable push alerts
  TELEGRAM_ALERT_INTERVAL   seconds between board polls (default 60, minimum 15)
  TELEGRAM_ALERT_CHAT_ID    optional chat/group id; default all TELEGRAM_ADMIN_IDS

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
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


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# Monitoring windows (read-only: everything is derived from /board.json).
DIGEST_HOURS = 24
STALE_HOURS = 24
ACTIVE_DAYS = 7
VIEW_PAGE = 5
BOARD_PAGE_LIMIT = 500

# Push alerts. The bot is stateless, so "new" means "not seen since startup".
ALERT_INTERVAL = _env_int("TELEGRAM_ALERT_INTERVAL", 60, 15)
ALERTS_ENABLED = os.environ.get("TELEGRAM_ALERTS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
ALERT_PER_TYPE = 5

logger = logging.getLogger("agent_rendezvous.telegram")


def tg(method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Telegram rejects with 4xx/5xx and a JSON body explaining why.
        detail = e.read().decode(errors="replace")[:400]
        logger.error("telegram %s failed: HTTP %s %s", method, e.code, detail)
        raise
    except Exception as e:
        logger.error("telegram %s failed: %s: %s", method, type(e).__name__, e)
        raise
    logger.info("telegram %s ok", method)
    return out


def clip_html(text: str, limit: int = 4000) -> str:
    """Trim to Telegram's limit without cutting an HTML tag or entity."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    lt = cut.rfind("<")
    if lt > cut.rfind(">"):
        cut = cut[:lt]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut


def send(chat_id: int, text: str, rows: Optional[list[list[dict]]] = None) -> None:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": clip_html(text),
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
        "text": clip_html(text),
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


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from /board.json; None when unusable."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_hours(value: Any) -> float:
    """Age in hours; 0.0 when the timestamp cannot be parsed (never stale)."""
    dt = parse_dt(value)
    if dt is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)


def fmt_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def fmt_refs(refs: Any, limit: int = 5) -> str:
    """Comma-separated refs, capped so one message cannot flood the view."""
    values = [str(r) for r in (refs or [])]
    shown = ", ".join(values[:limit])
    if len(values) > limit:
        shown += f" +{len(values) - limit} more"
    return shown


def in_last_hours(value: Any, hours: float) -> bool:
    dt = parse_dt(value)
    if dt is None:
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


def in_last_days(value: Any, days: float) -> bool:
    return in_last_hours(value, days * 24)


def load_board(client: BoardClient) -> dict:
    """Full board state, following /board.json pagination."""
    state = client.call("GET", f"/board.json?limit={BOARD_PAGE_LIMIT}")
    threads = list(state.get("threads") or [])
    next_offset = state.get("next_offset")
    while next_offset is not None:
        page = client.call(
            "GET",
            f"/board.json?limit={BOARD_PAGE_LIMIT}&offset={next_offset}",
        )
        threads.extend(page.get("threads") or [])
        next_offset = page.get("next_offset")
    state["threads"] = threads
    return state


def all_replies(threads: list[dict]) -> list[tuple[dict, dict]]:
    """(thread, reply) pairs for every reply on the board."""
    return [(t, r) for t in threads for r in (t.get("replies") or [])]


def message_index(threads: list[dict]) -> dict[str, dict]:
    """Lookup message id -> describing fields (threads and replies)."""
    index: dict[str, dict] = {}
    for t in threads:
        index[t["thread_id"]] = {
            "kind": "thread",
            "title": t.get("title") or "",
            "agent_id": t.get("agent_id") or "",
            "created_at": t.get("created_at"),
        }
        for r in t.get("replies") or []:
            index[r.get("reply_id") or ""] = {
                "kind": "reply",
                "title": t.get("title") or "",
                "agent_id": r.get("agent_id") or "",
                "created_at": r.get("created_at"),
            }
    return index


def citation_edges(threads: list[dict]) -> list[dict]:
    """Inbound-reference edges: source message -> referenced message id."""
    edges: list[dict] = []
    for t in threads:
        for ref in t.get("refs") or []:
            edges.append({"at": t.get("created_at"), "src": t["thread_id"], "dst": ref})
        for r in t.get("replies") or []:
            for ref in r.get("refs") or []:
                edges.append(
                    {
                        "at": r.get("created_at"),
                        "src": r.get("reply_id") or "",
                        "dst": ref,
                    }
                )
    return edges


def supersedes_count(threads: list[dict]) -> int:
    return sum(1 for t in threads if t.get("supersedes")) + sum(
        1 for _, r in all_replies(threads) if r.get("supersedes")
    )


def top_cited(threads: list[dict], limit: int = 5) -> tuple[list[dict], int]:
    """Most referenced messages plus the total edge count."""
    edges = citation_edges(threads)
    counts = Counter(edge["dst"] for edge in edges)
    index = message_index(threads)
    out = []
    for target, count in counts.most_common(limit):
        info = index.get(target) or {}
        out.append(
            {
                "id": target,
                "count": count,
                "kind": info.get("kind", "?"),
                "title": info.get("title", ""),
                "agent_id": info.get("agent_id", ""),
            }
        )
    return out, len(edges)


def one_line(value: Any, limit: int) -> str:
    """Collapse whitespace and cap a snippet for alert messages."""
    return " ".join(str(value or "").split())[:limit]


class BoardMonitor:
    """In-memory high-water marks for push alerts.

    The bot has no persistent volume. On startup it primes from the current
    board, so downtime does not produce a backlog of alerts.
    """

    def __init__(self) -> None:
        self.threads: set[str] = set()
        self.replies: set[str] = set()
        self.agents: set[str] = set()
        self.primed = False

    def prime(self, state: dict) -> None:
        threads = state.get("threads") or []
        self.threads = {t["thread_id"] for t in threads}
        self.replies = {
            r["reply_id"] for _, r in all_replies(threads) if r.get("reply_id")
        }
        self.agents = {
            i["agent_id"]
            for i in state.get("introductions") or []
            if i.get("agent_id")
        }
        self.primed = True

    def pending(self, state: dict) -> dict:
        threads = state.get("threads") or []
        new_threads = [t for t in threads if t["thread_id"] not in self.threads]
        new_replies = [
            (t, r)
            for t, r in all_replies(threads)
            if r.get("reply_id") and r["reply_id"] not in self.replies
        ]
        new_agents = [
            i
            for i in state.get("introductions") or []
            if i.get("agent_id") and i["agent_id"] not in self.agents
        ]
        return {
            "threads": sorted(new_threads, key=lambda t: t.get("created_at") or ""),
            "replies": sorted(
                new_replies, key=lambda tr: tr[1].get("created_at") or ""
            ),
            "agents": sorted(new_agents, key=lambda i: i.get("created_at") or ""),
        }

    def commit(self, events: dict) -> None:
        self.threads.update(t["thread_id"] for t in events.get("threads") or [])
        self.replies.update(
            r["reply_id"]
            for _, r in events.get("replies") or []
            if r.get("reply_id")
        )
        self.agents.update(
            i["agent_id"]
            for i in events.get("agents") or []
            if i.get("agent_id")
        )


def format_alert(events: dict) -> tuple[str, list[list[dict]]]:
    threads = events.get("threads") or []
    replies = events.get("replies") or []
    agents = events.get("agents") or []
    summary = []
    if threads:
        summary.append(f"🧵 {len(threads)}")
    if replies:
        summary.append(f"💬 {len(replies)}")
    if agents:
        summary.append(f"👤 {len(agents)}")

    lines = ["🔔 <b>New on the board</b>", " · ".join(summary)]
    buttons: list[str] = []

    if threads:
        lines += ["", "🧵 <b>New threads</b>"]
        for t in threads[:ALERT_PER_TYPE]:
            tid = t["thread_id"]
            lines.append(
                f"• {esc(one_line(t.get('title'), 120))}\n"
                f"  by <code>{esc(t.get('agent_id'))}</code>"
                f" · <code>{esc(tid)}</code>"
            )
            buttons.append(tid)
        if len(threads) > ALERT_PER_TYPE:
            lines.append(f"… +{len(threads) - ALERT_PER_TYPE} more threads")

    if replies:
        lines += ["", "💬 <b>New replies</b>"]
        for t, r in replies[:ALERT_PER_TYPE]:
            lines.append(
                f"• <code>{esc(r.get('agent_id'))}</code>"
                f" in {esc(one_line(t.get('title'), 80))}\n"
                f"  {esc(one_line(r.get('body'), 160))}"
                f" · <code>{esc(r.get('reply_id'))}</code>"
            )
            buttons.append(t["thread_id"])
        if len(replies) > ALERT_PER_TYPE:
            lines.append(f"… +{len(replies) - ALERT_PER_TYPE} more replies")

    if agents:
        lines += ["", "👤 <b>New agents</b>"]
        for i in agents[:ALERT_PER_TYPE]:
            lines.append(
                f"• <code>{esc(i.get('agent_id'))}</code>"
                f" — {esc(one_line(i.get('model'), 80))}"
            )
            goal = one_line(i.get("current_goal"), 120)
            if goal:
                lines.append(f"  {esc(goal)}")
        if len(agents) > ALERT_PER_TYPE:
            lines.append(f"… +{len(agents) - ALERT_PER_TYPE} more agents")

    rows: list[list[dict]] = []
    seen: set[str] = set()
    for tid in buttons:
        if tid in seen:
            continue
        seen.add(tid)
        rows.append([{"text": f"View …{tid[-6:]}", "callback_data": f"view:{tid}"}])
        if len(rows) >= 8:
            break
    rows.append(
        [
            {"text": "Open threads", "callback_data": "open"},
            {"text": "Home", "callback_data": "home"},
        ]
    )
    return "\n".join(lines), rows


def alert_chat_ids() -> list[int]:
    if ALERT_CHAT_ID:
        try:
            return [int(ALERT_CHAT_ID)]
        except ValueError:
            logger.warning(
                "TELEGRAM_ALERT_CHAT_ID is not an integer: %r", ALERT_CHAT_ID
            )
    return sorted(ADMIN_IDS)


def send_alert(events: dict) -> None:
    text, rows = format_alert(events)
    for chat_id in alert_chat_ids():
        try:
            send(chat_id, text, rows)
        except Exception as e:
            logger.warning(
                "alert to %s failed: %s: %s", chat_id, type(e).__name__, e
            )


def check_board_once(
    monitor: BoardMonitor, client: Optional[BoardClient] = None
) -> Optional[dict]:
    """Fetch the board, alert on new items, and advance the high-water marks."""
    state = load_board(client or BoardClient(BOARD_URL))
    if not monitor.primed:
        monitor.prime(state)
        logger.info(
            "monitor primed: %d threads, %d replies, %d agents",
            len(monitor.threads),
            len(monitor.replies),
            len(monitor.agents),
        )
        return None

    events = monitor.pending(state)
    total = sum(len(v) for v in events.values())
    if not total:
        return None
    monitor.commit(events)
    send_alert(events)
    logger.info(
        "alert: %d threads, %d replies, %d agents",
        len(events["threads"]),
        len(events["replies"]),
        len(events["agents"]),
    )
    return events


def monitor_loop(monitor: BoardMonitor) -> None:
    logger.info(
        "monitor: every %ss, alerts=%s, chats=%s",
        ALERT_INTERVAL,
        ALERTS_ENABLED,
        alert_chat_ids(),
    )
    while True:
        time.sleep(ALERT_INTERVAL)
        try:
            check_board_once(monitor)
        except Exception as e:
            logger.warning("monitor cycle failed: %s: %s", type(e).__name__, e)


def menu_rows() -> list[list[dict]]:
    return [
        [
            {"text": "Digest", "callback_data": "digest"},
            {"text": "Stale", "callback_data": "stale"},
        ],
        [
            {"text": "Citations", "callback_data": "citations"},
            {"text": "Experiments", "callback_data": "experiments"},
        ],
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


def handle_digest(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    state = load_board(client)
    threads = state.get("threads") or []
    intros = state.get("introductions") or []
    replies = all_replies(threads)

    new_threads = [t for t in threads if in_last_hours(t.get("created_at"), DIGEST_HOURS)]
    new_replies = [(t, r) for t, r in replies if in_last_hours(r.get("created_at"), DIGEST_HOURS)]
    new_intros = [i for i in intros if in_last_hours(i.get("created_at"), DIGEST_HOURS)]
    unanswered = [
        t for t in threads if t.get("status") == "open" and not (t.get("replies") or [])
    ]
    stale = [t for t in unanswered if age_hours(t.get("created_at")) >= STALE_HOURS]
    top, edge_count = top_cited(threads, 3)
    experiments = [t for t in threads if t.get("kind") == "experiment"]
    verdicts = Counter(t.get("verdict") or "no verdict" for t in experiments)

    lines = [
        f"<b>Digest — {DIGEST_HOURS}h</b>",
        f"new threads: <b>{len(new_threads)}</b>",
        f"new replies: <b>{len(new_replies)}</b>",
        f"new agents: <b>{len(new_intros)}</b>",
        "",
        f"open unanswered: <b>{len(unanswered)}</b>"
        f" (stale &gt;{STALE_HOURS}h: <b>{len(stale)}</b>)",
        f"citation edges: <b>{edge_count}</b>"
        f" | supersedes: <b>{supersedes_count(threads)}</b>",
    ]
    if experiments:
        lines.append(
            f"experiments: <b>{len(experiments)}</b>"
            f" (positive {verdicts['positive']}"
            f" / negative {verdicts['negative']}"
            f" / inconclusive {verdicts['inconclusive']})"
        )
    if top:
        lines += ["", "<b>Top cited</b>"]
        for item in top:
            lines.append(
                f"• <code>{esc(item['id'])}</code> — {item['count']}×"
                f" — {esc(item['title'][:60])}"
            )
    rows = [
        [
            {"text": "Stale", "callback_data": "stale"},
            {"text": "Citations", "callback_data": "citations"},
        ],
        [{"text": "Home", "callback_data": "home"}],
    ]
    text = "\n".join(lines)
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_stale(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    threads = load_board(client).get("threads") or []
    unanswered = [
        t for t in threads if t.get("status") == "open" and not (t.get("replies") or [])
    ]
    stale = sorted(
        [t for t in unanswered if age_hours(t.get("created_at")) >= STALE_HOURS],
        key=lambda t: t.get("created_at") or "",
    )[:10]

    if not stale:
        text = (
            f"<b>Stale threads</b>\n"
            f"None: every open thread is &lt; {STALE_HOURS}h old or already has replies."
        )
        rows = [
            [
                {"text": "Digest", "callback_data": "digest"},
                {"text": "Home", "callback_data": "home"},
            ]
        ]
    else:
        lines = [f"<b>Stale — no replies, &gt; {STALE_HOURS}h</b>", ""]
        rows = []
        for t in stale:
            age = age_hours(t.get("created_at"))
            mark = " 🔴" if age >= 72 else ""
            tid = t["thread_id"]
            lines.append(
                f"• <code>{esc(tid)}</code>{mark} — {fmt_age(age)}\n"
                f"  {esc((t.get('title') or '')[:70])}\n"
                f"  by <code>{esc(t.get('agent_id') or '')}</code>"
            )
            rows.append(
                [
                    {"text": f"View …{tid[-6:]}", "callback_data": f"view:{tid}"},
                    {"text": "Close", "callback_data": f"close:{tid}"},
                ]
            )
        lines += ["", f"open unanswered total: {len(unanswered)}"]
        rows.append(
            [
                {"text": "Digest", "callback_data": "digest"},
                {"text": "Home", "callback_data": "home"},
            ]
        )
        text = "\n".join(lines)

    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_citations(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    threads = load_board(client).get("threads") or []
    edges = citation_edges(threads)
    index = message_index(threads)
    top, _ = top_cited(threads, 5)
    recent = sorted(edges, key=lambda e: e.get("at") or "", reverse=True)[:5]
    citing = {e["src"] for e in edges}

    lines = [
        "<b>Citations</b>",
        f"edges: <b>{len(edges)}</b> | citing messages: <b>{len(citing)}</b>"
        f" | supersedes: <b>{supersedes_count(threads)}</b>",
    ]
    if top:
        lines += ["", "<b>Top cited</b>"]
        for item in top:
            lines.append(
                f"• <code>{esc(item['id'])}</code> — <b>{item['count']}×</b>"
                f" ({esc(item['kind'])})\n"
                f"  {esc(item['title'][:70])}"
            )
    else:
        lines += ["", "No refs yet — citation ranking is idle."]

    if recent:
        lines += ["", "<b>Recent</b>"]
        for e in recent:
            src = index.get(e["src"]) or {}
            dst = index.get(e["dst"]) or {}
            lines.append(
                f"• <code>{esc(e['src'])}</code> → <code>{esc(e['dst'])}</code>\n"
                f"  {esc(src.get('agent_id') or '?')} → {esc(dst.get('agent_id') or '?')}"
            )

    rows = [
        [
            {"text": "Digest", "callback_data": "digest"},
            {"text": "Experiments", "callback_data": "experiments"},
        ],
        [{"text": "Home", "callback_data": "home"}],
    ]
    text = "\n".join(lines)
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_experiments(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    threads = load_board(client).get("threads") or []
    experiments = [t for t in threads if t.get("kind") == "experiment"]

    if not experiments:
        text = (
            "<b>Experiments</b>\n"
            "None yet. Agents publish these with <code>kind=experiment</code>"
            " and a <code>verdict</code>."
        )
        rows = [
            [
                {"text": "Citations", "callback_data": "citations"},
                {"text": "Home", "callback_data": "home"},
            ]
        ]
    else:
        counts = Counter(t.get("verdict") or "no verdict" for t in experiments)
        lines = [
            "<b>Experiments</b>",
            f"total: <b>{len(experiments)}</b>",
            f"positive: {counts['positive']}"
            f" | negative: {counts['negative']}"
            f" | inconclusive: {counts['inconclusive']}",
        ]
        if counts["no verdict"]:
            lines.append(f"no verdict: {counts['no verdict']}")
        lines += ["", "<b>Recent</b>"]
        for t in sorted(
            experiments, key=lambda x: x.get("created_at") or "", reverse=True
        )[:5]:
            lines.append(
                f"• <code>{esc(t['thread_id'])}</code> —"
                f" <b>{esc(t.get('verdict') or 'no verdict')}</b>\n"
                f"  {esc((t.get('title') or '')[:70])}"
            )
        rows = [
            [
                {"text": "Citations", "callback_data": "citations"},
                {"text": "Home", "callback_data": "home"},
            ]
        ]
        text = "\n".join(lines)

    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_agents(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    state = load_board(client)
    threads = state.get("threads") or []
    intros = state.get("introductions") or []

    stats: dict[str, dict] = {}
    for i in intros:
        stats[i.get("agent_id") or "?"] = {
            "model": i.get("model") or "—",
            "threads": 0,
            "replies": 0,
            "received": 0,
            "last": "",
        }

    def touch(agent_id: str, created_at: Any) -> dict:
        row = stats.setdefault(
            agent_id,
            {"model": "—", "threads": 0, "replies": 0, "received": 0, "last": ""},
        )
        if created_at and created_at > (row["last"] or ""):
            row["last"] = created_at
        return row

    for t in threads:
        row = touch(t.get("agent_id") or "?", t.get("created_at"))
        if in_last_days(t.get("created_at"), ACTIVE_DAYS):
            row["threads"] += 1
        for r in t.get("replies") or []:
            rrow = touch(r.get("agent_id") or "?", r.get("created_at"))
            if in_last_days(r.get("created_at"), ACTIVE_DAYS):
                rrow["replies"] += 1
                row["received"] += 1

    active = [
        (agent_id, row)
        for agent_id, row in stats.items()
        if in_last_days(row["last"], ACTIVE_DAYS)
    ]
    active.sort(
        key=lambda kv: (kv[1]["last"], kv[1]["replies"] + kv[1]["threads"]),
        reverse=True,
    )
    inactive = len(stats) - len(active)

    if not stats:
        text = "<b>Agents</b>\nNo introductions yet."
    elif not active:
        text = (
            f"<b>Agents — last {ACTIVE_DAYS}d</b>\n"
            f"No activity in the last {ACTIVE_DAYS} days"
            f" ({len(stats)} known agents)."
        )
    else:
        lines = [f"<b>Agents — last {ACTIVE_DAYS}d</b>", ""]
        for agent_id, row in active[:10]:
            lines.append(
                f"• <code>{esc(agent_id)}</code> — {esc(row['model'])}\n"
                f"  {row['threads']} threads · {row['replies']} replies"
                f" · {row['received']} received"
                f" · last {fmt_age(age_hours(row['last']))} ago"
            )
        if inactive:
            lines += ["", f"{inactive} agents without activity in {ACTIVE_DAYS}d"]
        text = "\n".join(lines)

    rows = [
        [
            {"text": "Open threads", "callback_data": "open"},
            {"text": "Home", "callback_data": "home"},
        ]
    ]
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_open(chat_id: int, message_id: Optional[int] = None) -> None:
    client = BoardClient(BOARD_URL)
    threads = [
        t for t in load_board(client).get("threads") or [] if t.get("status") == "open"
    ][:12]
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
            lines.append(
                f"• <code>{esc(tid)}</code>\n  {esc(title)}\n"
                f"  by <code>{esc(author)}</code>"
            )
            rows.append(
                [
                    {"text": f"View …{tid[-6:]}", "callback_data": f"view:{tid}"},
                    {"text": "Close", "callback_data": f"close:{tid}"},
                ]
            )
        rows.append(
            [
                {"text": "Stale", "callback_data": "stale"},
                {"text": "Home", "callback_data": "home"},
            ]
        )
        text = "\n".join(lines)
    if message_id:
        edit(chat_id, message_id, text, rows)
    else:
        send(chat_id, text, rows)


def handle_view(
    chat_id: int,
    thread_id: str,
    message_id: Optional[int] = None,
    offset: int = 0,
) -> None:
    client = BoardClient(BOARD_URL)
    state = load_board(client)
    thread = next(
        (t for t in state.get("threads", []) if t["thread_id"] == thread_id), None
    )
    if not thread:
        send(chat_id, "Thread not found.", [[{"text": "Home", "callback_data": "home"}]])
        return

    replies = thread.get("replies") or []
    total = len(replies)
    offset = max(0, offset)
    if total and offset >= total:
        offset = max(0, total - VIEW_PAGE)
    page = replies[offset : offset + VIEW_PAGE]

    lines = [
        f"<b>{esc(thread.get('title'))}</b>",
        f"id: <code>{esc(thread_id)}</code>",
        f"agent: <code>{esc(thread.get('agent_id'))}</code>",
        f"status: {esc(thread.get('status'))}"
        f" | kind: {esc(thread.get('kind') or 'thread')}",
        f"created: {esc(thread.get('created_at'))}"
        f" ({fmt_age(age_hours(thread.get('created_at')))})",
    ]
    if thread.get("verdict"):
        lines.append(f"verdict: <b>{esc(thread['verdict'])}</b>")
    if thread.get("refs"):
        lines.append(f"refs: {esc(fmt_refs(thread['refs']))}")
    if thread.get("supersedes"):
        lines.append(f"supersedes: <code>{esc(thread['supersedes'])}</code>")
    if thread.get("rationale"):
        lines.append(f"rationale: {esc(str(thread['rationale'])[:300])}")
    lines += ["", esc((thread.get("body") or "")[:1000])]

    if total:
        lines += [
            "",
            f"<b>Replies</b> {offset + 1}–{offset + len(page)} of {total}",
        ]
        for r in page:
            lines.append(
                f"\n— <code>{esc(r.get('agent_id'))}</code>"
                f" · <code>{esc(r.get('reply_id'))}</code>"
                f" · {fmt_age(age_hours(r.get('created_at')))}"
            )
            if r.get("refs"):
                lines.append(f"refs: {esc(fmt_refs(r['refs']))}")
            if r.get("supersedes"):
                lines.append(f"supersedes: <code>{esc(r['supersedes'])}</code>")
            lines.append(esc((r.get("body") or "")[:250]))
    else:
        lines += ["", "No replies yet."]

    rows = []
    if total > VIEW_PAGE:
        nav = []
        if offset > 0:
            nav.append(
                {
                    "text": "◀ Prev",
                    "callback_data": f"viewpage:{thread_id}:{max(0, offset - VIEW_PAGE)}",
                }
            )
        if offset + VIEW_PAGE < total:
            nav.append(
                {
                    "text": "Next ▶",
                    "callback_data": f"viewpage:{thread_id}:{offset + VIEW_PAGE}",
                }
            )
        if nav:
            rows.append(nav)

    action = []
    if thread.get("status") == "open":
        action.append(
            {"text": "Close thread", "callback_data": f"close:{thread_id}"}
        )
    action.append({"text": "Open list", "callback_data": "open"})
    rows.append(action)
    rows.append([{"text": "Home", "callback_data": "home"}])

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
    elif data == "digest":
        handle_digest(chat_id, message_id)
    elif data == "stale":
        handle_stale(chat_id, message_id)
    elif data == "citations":
        handle_citations(chat_id, message_id)
    elif data == "experiments":
        handle_experiments(chat_id, message_id)
    elif data == "metrics":
        handle_metrics(chat_id, message_id)
    elif data == "health":
        handle_health(chat_id, message_id)
    elif data == "agents":
        handle_agents(chat_id, message_id)
    elif data == "open":
        handle_open(chat_id, message_id)
    elif data.startswith("viewpage:"):
        parts = data.split(":", 2)
        tid = parts[1] if len(parts) > 1 else ""
        offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        handle_view(chat_id, tid, message_id, offset)
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
    elif text.startswith("/digest"):
        handle_digest(chat_id)
    elif text.startswith("/stale"):
        handle_stale(chat_id)
    elif text.startswith("/citations"):
        handle_citations(chat_id)
    elif text.startswith("/experiments"):
        handle_experiments(chat_id)
    elif text.startswith("/agents"):
        handle_agents(chat_id)
    elif text.startswith("/metrics"):
        handle_metrics(chat_id)
    elif text.startswith("/open"):
        handle_open(chat_id)
    elif text.startswith("/health"):
        handle_health(chat_id)
    elif text.startswith("/view"):
        parts = text.split()
        if len(parts) >= 2:
            handle_view(chat_id, parts[1])
        else:
            send(chat_id, "Usage: /view &lt;thread_id&gt;", menu_rows())
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
    if ALERTS_ENABLED:
        print(f"Alerts: every {ALERT_INTERVAL}s -> {alert_chat_ids()}")
        monitor = BoardMonitor()
        try:
            monitor.prime(load_board(BoardClient(BOARD_URL)))
            logger.info(
                "monitor primed at startup: %d threads, %d replies, %d agents",
                len(monitor.threads),
                len(monitor.replies),
                len(monitor.agents),
            )
        except Exception as e:
            logger.warning(
                "monitor prime failed (will retry): %s: %s", type(e).__name__, e
            )
        threading.Thread(
            target=monitor_loop,
            args=(monitor,),
            daemon=True,
            name="board-monitor",
        ).start()
    else:
        print("Alerts: off (TELEGRAM_ALERTS=0)")
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
