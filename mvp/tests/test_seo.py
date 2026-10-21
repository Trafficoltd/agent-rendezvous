"""P4: public thread pages, sitemap, and crawler policy."""

from __future__ import annotations


def _thread(client, title: str, body: str) -> str:
    return client.post(
        "/thread", json={"agent_id": "seo-1", "title": title, "body": body}
    ).json()["thread_id"]


def test_thread_page_is_html_and_escapes_untrusted_content(board):
    client, _ = board
    thread_id = _thread(
        client,
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(2)> a body long enough",
    )
    client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "helper-1", "body": "<b>not bold</b> an answer"},
    )

    page = client.get(f"/t/{thread_id}")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "&lt;img src=x onerror=alert(2)&gt;" in page.text
    assert "&lt;b&gt;not bold&lt;/b&gt;" in page.text
    assert "<script>alert" not in page.text
    assert "<img src=x" not in page.text
    assert f"/t/{thread_id}" in page.text
    assert "DiscussionForumPosting" in page.text


def test_thread_page_404_and_head(board):
    client, _ = board
    assert client.get("/t/thr-doesnotexist").status_code == 404

    thread_id = _thread(client, "head check", "a body long enough for head")
    head = client.head(f"/t/{thread_id}")
    assert head.status_code == 200
    assert head.content == b""
    assert int(head.headers["content-length"]) > 0


def test_sitemap_lists_root_and_every_thread(board):
    client, _ = board
    first = _thread(client, "sitemap one", "a body long enough for sitemap")
    second = _thread(client, "sitemap two", "another body long enough here")

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<urlset" in response.text
    assert f"/t/{first}" in response.text
    assert f"/t/{second}" in response.text
    assert "<lastmod>" in response.text
    assert "http://testserver/" in response.text


def test_sitemap_and_thread_pages_support_head(board):
    client, _ = board
    _thread(client, "head sitemap", "a body long enough for sitemap")
    assert client.head("/sitemap.xml").status_code == 200


def test_robots_advertises_the_sitemap(board):
    client, _ = board
    body = client.get("/robots.txt").text
    assert "Allow: /t/" in body
    assert "Sitemap: http://testserver/sitemap.xml" in body
