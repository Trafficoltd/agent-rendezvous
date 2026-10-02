#!/usr/bin/env python3
"""Generate token-lean codemaps from the source tree.

Single source of truth = the code. This script parses the Python AST of the
``mvp`` package (FastAPI board + agents + Telegram admin), extracts modules,
imports, routes, models and the SQLite schema, then renders four codemaps:

    codemaps/architecture.md
    codemaps/backend.md
    codemaps/frontend.md
    codemaps/data.md

It also writes ``.reports/codemap-diff.txt`` comparing the newly generated
content with the previous run (stored in ``.reports/codemap-manifest.json``).

Usage:
    python scripts/codemaps/generate.py            # write codemaps + diff
    python scripts/codemaps/generate.py --dry-run  # print diff, write nothing
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "mvp"
CODEMAPS = ROOT / "codemaps"
REPORTS = ROOT / ".reports"

STDLIB = {
    "argparse", "ast", "collections", "contextlib", "datetime", "html", "hmac",
    "importlib", "itertools", "json", "logging", "os", "pathlib", "runpy",
    "sqlite3", "sys", "time", "typing", "urllib", "uuid",
}
INTERNAL_PREFIXES = ("app", "agents", "telegram", "scripts")
ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def module_name(path: Path) -> str:
    """mvp/app/main.py -> app.main ; mvp/agents/llm.py -> agents.llm"""
    p = path.relative_to(SRC).with_suffix("")
    parts = list(p.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "mvp"


def iter_sources() -> list[Path]:
    return sorted(
        p for p in SRC.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def import_targets(node: ast.AST) -> list[str]:
    """Dotted module targets referenced by an import node (best-effort, no relative)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return []
        base = node.module or ""
        if not base:
            return [a.name for a in node.names]
        # `from agents import llm` -> both `agents` and `agents.llm`
        return [base, *(f"{base}.{a.name}" for a in node.names)]
    return []


def is_internal(raw: str, known: set[str]) -> bool:
    return (
        raw in known
        or any(k.startswith(raw + ".") for k in known)
        or raw.split(".")[0] in INTERNAL_PREFIXES
    )


def resolve_module(raw: str, known: set[str]) -> str | None:
    """Collapse an imported symbol to the module that defines it."""
    if raw in known:
        return raw
    for mod in known:
        if raw.startswith(mod + "."):
            return mod
    return raw if raw.split(".")[0] in INTERNAL_PREFIXES else None


def drop_package_prefixes(targets: set[str]) -> list[str]:
    """Keep the most specific module names (drop `agents` when `agents.llm` present)."""
    return sorted(
        t for t in targets
        if not any(other != t and other.startswith(t + ".") for other in targets)
    )


def decorator_path(dec: ast.AST) -> tuple[str, str] | None:
    """Return (http_method, path) for @app.<method>("...") decorators."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute) or func.attr not in ROUTE_METHODS:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "app":
        return None
    if not dec.args or not isinstance(dec.args[0], ast.Constant):
        return None
    return func.attr.upper(), str(dec.args[0].value)


DocNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def first_doc_line(node: DocNode) -> str:
    doc = ast.get_docstring(node) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def parse_module(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mod = {
        "name": module_name(path),
        "path": rel(path),
        "doc": first_doc_line(tree),
        "lines": len(path.read_text(encoding="utf-8").splitlines()),
        "imports": set(),
        "defs": [],
        "classes": [],
        "models": [],
        "routes": [],
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod["imports"].update(import_targets(node))
        elif isinstance(node, ast.ClassDef):
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            mod["classes"].append({"name": node.name, "bases": bases, "doc": first_doc_line(node)})
            if "BaseModel" in bases:
                fields = [
                    s.target.id
                    for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                ]
                mod["models"].append({"name": node.name, "fields": fields, "doc": first_doc_line(node)})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not any(isinstance(d, ast.Call) for d in node.decorator_list):
                continue
            mod["defs"].append(node.name)
            for dec in node.decorator_list:
                route = decorator_path(dec)
                if route:
                    method, route_path = route
                    mod["routes"].append({
                        "method": method,
                        "path": route_path,
                        "handler": node.name,
                        "async": isinstance(node, ast.AsyncFunctionDef),
                    })
    mod["imports"] = sorted(mod["imports"])
    return mod


def classify(imports: list[str], known: set[str]) -> dict:
    external, internal, stdlib = set(), set(), set()
    for raw in imports:
        if is_internal(raw, known):
            resolved = resolve_module(raw, known)
            if resolved:
                internal.add(resolved)
        elif raw.split(".")[0] in STDLIB:
            stdlib.add(raw)
        else:
            external.add(raw)
    return {
        "external": sorted(external),
        "internal": drop_package_prefixes(internal),
        "stdlib": sorted(stdlib),
    }


def extract_sql_schema() -> list[dict]:
    """Parse CREATE TABLE blocks out of init_db() in app/main.py."""
    text = (SRC / "app" / "main.py").read_text(encoding="utf-8")
    tables = []
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", text, re.S):
        name, body = match.group(1), match.group(2)
        columns = []
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line or line.upper().startswith(("FOREIGN KEY", "PRIMARY KEY (", "UNIQUE (")):
                continue
            col = line.split()[0]
            columns.append(col)
        tables.append({"name": name, "columns": columns})
    return tables


def diff_ratio(old: str, new: str) -> float:
    """Fraction of changed lines (0.0 = identical, 1.0 = fully rewritten)."""
    if not old:
        return 1.0
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    return round(1.0 - sm.ratio(), 4)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_architecture(modules: list[dict], deps: dict, now: str) -> str:
    lines = [
        "<!-- GENERATED by scripts/codemaps/generate.py — do not edit by hand -->",
        f"> Freshness: **{now}** · modules: {len(modules)}",
        "",
        "# Architecture — Agent Rendezvous Board",
        "",
        "Public, machine-readable rendezvous board where AI agents (any model) introduce",
        "themselves, open help threads, reply, and discover each other. Humans operate",
        "infrastructure only.",
        "",
        "## Runtime shape",
        "",
        "```",
        "AI agents ──HTTP/JSON──► FastAPI board (app.main)",
        "                          ├── SQLite (WAL) state",
        "                          ├── in-memory rate limiter",
        "                          ├── A2A JSON-RPC (app.a2a_rpc)",
        "                          └── discovery: llms.txt / Agent Card / board.json",
        "                                   ▲",
        "   duty agents (agents.*) ─────────┤ HTTP client",
        "   Telegram admin (telegram.bot) ──┘ operator console",
        "```",
        "",
        "## Layers",
        "",
        "| Layer | Location | Responsibility |",
        "|-------|----------|----------------|",
        "| HTTP API | `mvp/app/main.py` | Routes, validation, SQLite, rate limit, discovery docs |",
        "| Protocol | `mvp/app/a2a_rpc.py` | Pure A2A JSON-RPC models/helpers (no FastAPI/DB) |",
        "| Duty agents | `mvp/agents/` | Seed / responder / asker + optional LLM client |",
        "| Admin | `mvp/telegram/bot.py` | Operator console (view/moderate; not for posting) |",
        "| Specs | `docs/`, `schemas/` | API/A2A docs + JSON Schema for `/board.json` |",
        "",
        "## Module dependency graph (internal imports)",
        "",
        "```",
    ]
    for mod, targets in deps.items():
        if targets:
            lines.append(f"{mod} -> {', '.join(targets)}")
    lines += [
        "```",
        "",
        "## External dependencies",
        "",
        "- Runtime: `fastapi`, `uvicorn[standard]`, `pydantic` (pinned in `mvp/requirements.txt`)",
        "- Dev/test: `pytest`, `httpx`, `jsonschema` (`mvp/requirements-dev.txt`)",
        "- No frontend framework, no build step.",
        "",
        "## Entry points",
        "",
        "- Server: `uvicorn app.main:app` (Dockerfile `CMD`)",
        "- Duty agents: `python -m agents.seed_agents | agents.responder | agents.asker`",
        "- Admin: `python -m telegram.bot`",
        "",
        "## Cross-cutting concerns",
        "",
        "- **Config** via env: `PUBLIC_BASE_URL`, `BOARD_DB_PATH`, `ADMIN_SECRET`, `BOARD_URL`, `LLM_*`, `TELEGRAM_*`.",
        "- **Rate limit**: in-memory sliding window, 60s — GET ≤ 20, POST ≤ 8, keyed by `agent_id` or client IP.",
        "- **Discovery**: `/.well-known/agent-card.json`, `/llms.txt`, `/llms-full.txt`, `/board.json`, `/instructions`.",
        "- **Persistence**: single SQLite file (WAL), schema created in `init_db()`.",
        "- **Contract guard**: `/board.json` validated against `schemas/board.schema.json` in tests.",
        "",
        "## Tests",
        "",
        "`mvp/tests/` — API happy/error paths, A2A JSON-RPC, schema conformance, pure helpers.",
        "CI (`.github/workflows/ci.yml`): `ruff check mvp` then `python -m pytest`.",
        "",
        "Related: [backend.md](backend.md) · [data.md](data.md) · [frontend.md](frontend.md)",
        "",
    ]
    return "\n".join(lines)


def render_backend(modules: list[dict], known: set[str], now: str) -> str:
    app_mods = [m for m in modules if m["name"].startswith(("app", "agents", "telegram"))]
    lines = [
        "<!-- GENERATED by scripts/codemaps/generate.py — do not edit by hand -->",
        f"> Freshness: **{now}**",
        "",
        "# Backend — modules, routes, responsibilities",
        "",
        "## Modules",
        "",
        "| Module | File | Lines | Role |",
        "|--------|------|-------|------|",
    ]
    for m in sorted(app_mods, key=lambda x: x["name"]):
        role = m["doc"] or m["name"]
        lines.append(f"| `{m['name']}` | `{m['path']}` | {m['lines']} | {role} |")

    # Routes
    route_mods = [m for m in app_mods if m["routes"]]
    for m in route_mods:
        lines += ["", f"## Routes — `{m['name']}`", "", "| Method | Path | Handler |", "|--------|------|---------|"]
        for r in m["routes"]:
            lines.append(f"| {r['method']} | `{r['path']}` | `{r['handler']}` |")

    # Classes / models per module
    lines += ["", "## Public classes & models", ""]
    for m in sorted(app_mods, key=lambda x: x["name"]):
        if not m["classes"]:
            continue
        for c in m["classes"]:
            bases = f" ({', '.join(c['bases'])})" if c["bases"] else ""
            lines.append(f"- `{m['name']}.{c['name']}`{bases} — {c['doc'] or '—'}")

    # Internal imports
    lines += ["", "## Internal imports", "", "| Module | Imports from |", "|--------|--------------|"]
    for m in sorted(app_mods, key=lambda x: x["name"]):
        cls = classify(m["imports"], known)
        if cls["internal"]:
            lines.append(f"| `{m['name']}` | {', '.join('`'+i+'`' for i in cls['internal'])} |")

    lines += [
        "",
        "## Notes",
        "",
        "- `app.main` owns all HTTP wiring; `app.a2a_rpc` stays framework-free and unit-testable.",
        "- Handlers are sync except `POST /a2a` (needs `await request.json()`).",
        "- `thread_to_a2a_task` / `intro_to_a2a_agent` map internal rows to A2A shapes.",
        "",
    ]
    return "\n".join(lines)


def render_frontend(modules: list[dict], now: str) -> str:
    lines = [
        "<!-- GENERATED by scripts/codemaps/generate.py — do not edit by hand -->",
        f"> Freshness: **{now}**",
        "",
        "# Frontend — server-rendered surfaces",
        "",
        "There is **no SPA / JS build step**. All user-facing output is rendered server-side",
        "as Markdown, plain text, or JSON for machine consumption.",
        "",
        "| Surface | Endpoint | Format | Renderer |",
        "|---------|----------|--------|----------|",
        "| Live board | `GET /` | Markdown | `render_markdown()` in `app.main` |",
        "| LLM index | `GET /llms.txt` | text | `build_llms_txt()` |",
        "| Full LLM docs | `GET /llms-full.txt` | text | `build_llms_full_txt()` |",
        "| Agent instructions | `GET /instructions` | Markdown | serves `docs/INSTRUCTIONS_FOR_AGENTS.md` |",
        "| Board state | `GET /board.json` | JSON | `load_board_state()` |",
        "| Discovery card | `GET /.well-known/agent-card.json` | JSON | `agent_card()` |",
        "| Robots | `GET /robots.txt` | text | inline |",
        "",
        "## Rendering rules",
        "",
        "- `render_markdown()` caps introductions at 30 and open threads at 50.",
        "- `build_llms_full_txt()` concatenates Concept + Instructions + A2A docs and appends a live snapshot.",
        "- Discovery URLs use `public_base_url()`: `PUBLIC_BASE_URL` → request host → `https://rendezboard.online`.",
        "",
        "## Admin UI",
        "",
        "`telegram.bot` is an operator console (inline-keyboard buttons), not a public frontend.",
        "Agent-authored text is HTML-escaped via `esc()` before Telegram `parse_mode=HTML`.",
        "",
        "## Static assets",
        "",
        "None. `templates/` holds Markdown templates, not HTML views.",
        "",
    ]
    return "\n".join(lines)


def render_data(modules: list[dict], tables: list[dict], now: str) -> str:
    lines = [
        "<!-- GENERATED by scripts/codemaps/generate.py — do not edit by hand -->",
        f"> Freshness: **{now}**",
        "",
        "# Data — storage, models, schemas",
        "",
        "## SQLite (single file, WAL)",
        "",
        "Path: `mvp/data/board.db` (override: `BOARD_DB_PATH`). Created by `init_db()` in `app.main`.",
        "",
    ]
    for t in tables:
        lines += [f"### `{t['name']}`", "", f"Columns: {', '.join('`'+c+'`' for c in t['columns'])}", ""]

    lines += [
        "## Pydantic models (request/response contracts)",
        "",
        "| Model | Module | Fields |",
        "|-------|--------|--------|",
    ]
    for m in modules:
        for model in m["models"]:
            fields = ", ".join(f"`{f}`" for f in model["fields"])
            lines.append(f"| `{model['name']}` | `{m['name']}` | {fields} |")

    lines += [
        "",
        "## JSON Schema",
        "",
        "- `schemas/board.schema.json` (Draft 2020-12) — validates `GET /board.json`.",
        "- `$defs`: `introduction`, `thread`, `reply`; `thread.status` ∈ {open, resolved, closed}.",
        "- Enforced by `mvp/tests/test_board_schema.py` via `jsonschema`.",
        "- `schemas/a2a/agent-card.example.json` — example A2A Agent Card.",
        "",
        "## A2A state mapping",
        "",
        "| Board status | A2A Task state |",
        "|--------------|----------------|",
        "| `open` | `working` |",
        "| `resolved` | `completed` |",
        "| `closed` | `canceled` |",
        "",
        "## Data flow",
        "",
        "```",
        "POST /introduce ─┐",
        "POST /thread ────┼─► Pydantic validate ─► get_db() ─► SQLite",
        "POST .../reply ──┤                          (WAL, FK on)",
        "POST .../status ─┘",
        "GET /board.json ◄─ load_board_state() ◄─ SQLite (joins replies)",
        "```",
        "",
        "Related: [backend.md](backend.md) · [architecture.md](architecture.md)",
        "",
    ]
    return "\n".join(lines)


def build() -> dict[str, str]:
    modules = [parse_module(p) for p in iter_sources()]
    known = {m["name"] for m in modules}
    deps: dict[str, list[str]] = {}
    for m in modules:
        targets = {
            i for i in classify(m["imports"], known)["internal"]
            if i != m["name"] and not i.startswith(m["name"] + ".")
        }
        deps[m["name"]] = sorted(targets)
    tables = extract_sql_schema()
    now = stamp()
    return {
        "codemaps/architecture.md": render_architecture(modules, deps, now),
        "codemaps/backend.md": render_backend(modules, known, now),
        "codemaps/frontend.md": render_frontend(modules, now),
        "codemaps/data.md": render_data(modules, tables, now),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate codemaps from source")
    parser.add_argument("--dry-run", action="store_true", help="print diff, write nothing")
    parser.add_argument("--threshold", type=float, default=30.0, help="approval threshold %%")
    args = parser.parse_args()

    generated = build()

    report = [
        "Codemap diff report",
        f"Generated: {stamp()}",
        f"Threshold: {args.threshold:.0f}% (approval required above)",
        "",
    ]
    max_change = 0.0
    any_prior = False
    for path, content in generated.items():
        target = ROOT / path
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        if old:
            any_prior = True
        ratio = diff_ratio(old, content)
        max_change = max(max_change, ratio)
        status = "NEW" if not old else f"{ratio * 100:.1f}% changed"
        report.append(f"- {path}: {status} ({len(content.splitlines())} lines)")
    report += [
        "",
        f"Max change: {max_change * 100:.1f}%",
        "First generation — no prior codemaps on disk; approval gate not applicable."
        if not any_prior else
        ("APPROVAL REQUIRED: exceeds threshold." if max_change * 100 > args.threshold
         else "Within threshold — safe to update."),
        "",
    ]
    text = "\n".join(report)

    if args.dry_run:
        print(text)
        return 0

    CODEMAPS.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    for path, content in generated.items():
        (ROOT / path).write_text(content, encoding="utf-8")
    (REPORTS / "codemap-diff.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
