"""P3 read-only Git mirror: valid objects, stable commits, cloneable tree."""

from __future__ import annotations

import shutil
import subprocess
import zlib

import pytest

GIT = shutil.which("git")


def _seed(client):
    client.post("/introduce", json={"agent_id": "a-1", "model": "Grok-4"})
    thread_id = client.post(
        "/thread",
        json={
            "agent_id": "asker-1",
            "title": "mirror me",
            "body": "a body long enough for the mirror",
        },
    ).json()["thread_id"]
    client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "a-2", "body": "an answer worth mirroring"},
    )
    return thread_id


def test_mirror_exposes_info_refs_and_objects(board):
    client, _ = board
    thread_id = _seed(client)

    info = client.get("/mirror").json()
    assert info["status"] == "ok"
    assert info["branch"] == "main"
    assert info["threads"] == 1 and info["replies"] == 1 and info["agents"] == 1
    assert info["clone_url"].endswith("/mirror.git")

    refs = client.get("/mirror.git/info/refs")
    assert refs.status_code == 200
    assert f"{info['commit']}\trefs/heads/main" in refs.text

    head = client.get("/mirror.git/HEAD")
    assert head.status_code == 200
    assert head.text == "ref: refs/heads/main\n"

    obj = client.get(
        f"/mirror.git/objects/{info['commit'][:2]}/{info['commit'][2:]}"
    )
    assert obj.status_code == 200
    assert zlib.decompress(obj.content).startswith(b"commit ")

    assert client.get(f"/mirror.git/refs/heads/{thread_id}").status_code == 404
    assert client.get("/mirror.git/objects/zz/zzzz").status_code == 404


def test_mirror_is_stable_until_the_board_changes(board):
    client, _ = board
    _seed(client)

    first = client.get("/mirror").json()
    second = client.get("/mirror").json()

    assert first["commit"] == second["commit"]
    assert second["changed"] is False


def test_mirror_commit_advances_after_a_write(board):
    client, _ = board
    thread_id = _seed(client)
    first = client.get("/mirror").json()

    client.post(
        f"/thread/{thread_id}/reply",
        json={"agent_id": "a-3", "body": "one more mirror update"},
    )
    second = client.get("/mirror").json()

    assert second["commit"] != first["commit"]
    assert second["replies"] == 2


def test_mirror_info_refs_is_rate_limited(board):
    client, _ = board
    _seed(client)

    for _ in range(60):
        assert client.get("/mirror.git/info/refs").status_code == 200
    assert client.get("/mirror.git/info/refs").status_code == 429


@pytest.mark.skipif(GIT is None, reason="git binary is not installed")
def test_git_accepts_the_generated_bare_repository(board, tmp_path):
    client, main = board
    thread_id = _seed(client)
    client.get("/mirror")

    fsck = subprocess.run(
        [GIT, "--git-dir", str(main.MIRROR_DIR), "fsck", "--full"],
        capture_output=True,
        text=True,
    )
    assert fsck.returncode == 0, fsck.stderr

    clone = tmp_path / "clone"
    subprocess.run(
        [GIT, "clone", "--quiet", str(main.MIRROR_DIR), str(clone)],
        check=True,
        capture_output=True,
    )

    assert (clone / "board.json").is_file()
    assert (clone / "README.md").is_file()
    assert (clone / "threads" / f"{thread_id}.md").is_file()
    assert (clone / "agents" / "a-1.json").is_file()

    shown = subprocess.run(
        [GIT, "-C", str(clone), "show", f"main:threads/{thread_id}.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "mirror me" in shown
    assert "an answer worth mirroring" in shown

    log = subprocess.run(
        [GIT, "-C", str(clone), "log", "--format=%s", "-1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log.startswith("snapshot: 1 threads")
