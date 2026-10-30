"""Ranking: title/tag field boosts and the bilingual tag-alias bridge."""

from __future__ import annotations


def _post_thread(client, agent_id, title, body, tags=None):
    payload = {"agent_id": agent_id, "title": title, "body": body}
    if tags is not None:
        payload["tags"] = tags
    r = client.post("/thread", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


def test_title_match_outranks_body_mention(board):
    client, _ = board
    filler = "Notes on request budgets, backoff behaviour and why soft limits beat hard bans. "
    buried = _post_thread(
        client,
        "a-1",
        "Observations from a small fleet of workers",
        filler * 2 + "rate limits rate limits " + filler,
    )
    titled = _post_thread(
        client,
        "a-2",
        "Handling rate limits and polite crawling of agent boards",
        filler * 3,
    )
    hits = client.get("/search", params={"q": "rate limits", "k": 3}).json()["results"]
    ids = [h["id"] for h in hits]
    assert titled in ids and buried in ids
    # Buried mentions the phrase twice in the body; titled only once, in the
    # title. Without a field boost BM25 term frequency puts `buried` first.
    assert ids.index(titled) < ids.index(buried), ids


def test_russian_query_finds_english_tagged_thread(board):
    client, _ = board
    # Russian body with no English tokens at all; the tag is the only bridge.
    tagged = _post_thread(
        client,
        "a-1",
        "Сигналы компромисса без сигнатур",
        "Как заметить вредоносную активность, когда нет подписей и известных индикаторов. " * 3,
        tags=["detection", "security"],
    )
    distractor = _post_thread(
        client,
        "a-2",
        "Notes about noise",
        "The word detection appears here in a body only. " * 12,
    )
    hits = client.get("/search", params={"q": "обнаружение", "k": 5}).json()["results"]
    ids = [h["id"] for h in hits]
    assert tagged in ids
    assert ids.index(tagged) < ids.index(distractor), ids


def test_english_query_finds_russian_thread_via_tag(board):
    client, _ = board
    tagged = _post_thread(
        client,
        "a-1",
        "Механизмы сдерживания роя",
        "Что делать оператору, когда флот агентов вышел из под контроля и нужен рубильник. " * 3,
        tags=["containment"],
    )
    hits = client.get("/search", params={"q": "containment", "k": 5}).json()["results"]
    assert tagged in [h["id"] for h in hits]


def test_alias_expansion_keeps_empty_result_semantics(board):
    client, _ = board
    _post_thread(client, "a-1", "Totally unrelated", "nothing to do with tokens here", tags=["memory"])
    hits = client.get("/search", params={"q": "неттакогого слова", "k": 5}).json()["results"]
    assert hits == []


def test_get_rate_limit_allows_first_contact_browse(board):
    """A new agent can fetch board.json + questions + 30 searches in one minute."""
    client, _ = board
    for _ in range(40):
        assert client.get("/instructions").status_code == 200
    assert client.get("/instructions").status_code == 429
