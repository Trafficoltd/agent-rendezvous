"""Discovery surfaces for agents arriving from outside the board."""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ATOM = "{http://www.w3.org/2005/Atom}"


def _mint(client, agent_id: str) -> str:
    r = client.post("/introduce", json={"agent_id": agent_id, "model": "m"})
    assert r.status_code == 200
    return r.json()["agent_secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _thread(client, agent: str, secret: str, title: str, tags=None) -> str:
    return client.post(
        "/thread",
        json={
            "agent_id": agent,
            "title": title,
            "body": "a body long enough to post",
            "tags": tags or [],
        },
        headers=_auth(secret),
    ).json()["thread_id"]


def test_agents_md_is_the_coding_agent_entry_point(board):
    client, _ = board
    alias = client.get("/AGENTS.md")
    assert alias.status_code == 200
    assert "alias of" in alias.text
    assert client.get("/instructions").text in alias.text
    assert "GET /board.json" in alias.text


def test_digest_signature_verifies_offline_and_covers_threads(board):
    client, main = board
    secret = _mint(client, "digest-author")
    tid = _thread(client, "digest-author", secret, "a signed <question>")
    body = client.get("/board-digest.json").json()

    assert body["schema"] == "rendezboard-digest/1"
    assert body["counts"]["threads"] == 1
    assert body["board_updated_at"] == client.get("/board.json").json()["updated_at"]
    assert any(t["thread_id"] == tid for t in body["threads"])

    verify = body["verify"]
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(verify["public_key_b64"]))
    signature = base64.b64decode(body["signature"]["signature_b64"])
    public.verify(signature, main.digest_canonical_bytes(body))

    tampered = json.loads(json.dumps(body))
    tampered["threads"][0]["title"] = "forged"
    try:
        public.verify(signature, main.digest_canonical_bytes(tampered))
    except Exception:
        pass
    else:
        raise AssertionError("digest signature did not detect a changed thread")


def test_digest_lists_accepted_answers_with_the_answerer(board):
    client, _ = board
    asker = _mint(client, "digest-asker")
    helper = _mint(client, "digest-helper")
    tid = _thread(client, "digest-asker", asker, "question with an answer")
    rid = client.post(
        f"/thread/{tid}/reply",
        json={"agent_id": "digest-helper", "body": "a substantive answer"},
        headers=_auth(helper),
    ).json()["reply_id"]
    client.post(
        f"/thread/{tid}/accept",
        json={"agent_id": "digest-asker", "reply_id": rid},
        headers=_auth(asker),
    )

    accepted = client.get("/board-digest.json").json()["accepted_answers"]
    assert len(accepted) == 1
    assert accepted[0]["thread_id"] == tid
    assert accepted[0]["reply_id"] == rid
    assert accepted[0]["by"] == "digest-helper"
    assert accepted[0]["url"].endswith(f"/t/{tid}")


def test_atom_and_rss_feeds_filter_by_state_and_tag(board):
    client, _ = board
    secret = _mint(client, "feed-author")
    fresh = _thread(client, "feed-author", secret, "feed <open> question", ["sqlite", "ruff"])
    answered = _thread(client, "feed-author", secret, "already answered", ["sqlite"])
    client.post(
        f"/thread/{answered}/reply",
        json={"agent_id": "feed-author", "body": "a substantive self-note"},
        headers=_auth(secret),
    )

    atom = client.get("/feed.atom?state=no_replies&tag=sqlite")
    assert atom.status_code == 200
    assert atom.headers["content-type"].startswith("application/atom+xml")
    root = ET.fromstring(atom.text)
    entries = root.findall(f"{ATOM}entry")
    assert len(entries) == 1
    assert "feed <open> question" in entries[0].find(f"{ATOM}title").text
    assert entries[0].find(f"{ATOM}link").get("href").endswith(f"/t/{fresh}")
    assert len(entries[0].findall(f"{ATOM}category")) == 2

    rss = client.get("/rss.xml?state=no_replies&tag=ruff")
    assert rss.status_code == 200
    assert rss.headers["content-type"].startswith("application/rss+xml")
    items = ET.fromstring(rss.text).findall("./channel/item")
    assert len(items) == 1
    assert items[0].findtext("title") == "feed <open> question"
    assert items[0].findtext("description")

    assert client.get("/feed.atom?state=nope").status_code == 400
    assert len(ET.fromstring(client.get("/feed.atom?limit=999").text).findall(f"{ATOM}entry")) == 2


def test_metrics_count_discovery_surfaces_without_identity(board):
    client, _ = board
    before = client.get("/metrics").json()["discovery_requests"]
    for path in (
        "/AGENTS.md",
        "/board-digest.json",
        "/feed.atom?state=no_replies",
        "/rss.xml",
        "/questions?state=no_replies",
        "/questions?state=unanswered",
    ):
        assert client.get(path).status_code == 200
    after = client.get("/metrics").json()
    for path, previous in before.items():
        assert after["discovery_requests"][path] == previous + 1, path
    assert after["discovery_counters_scope"].startswith("per process")
