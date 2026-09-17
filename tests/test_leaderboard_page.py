"""Tests for GET /leaderboard/page: signed-cursor pagination.

The DB is shared with test_api.py when the whole suite runs (whichever test
module imports the app first fixes LEDGER_DB_PATH), so every test here uses
unique id prefixes and never assumes an empty ledger — except the ones that
explicitly point the DB at a fresh file.
"""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import uuid

# Point the DB at a temp file BEFORE importing the app (no-op if test_api
# already fixed it), and make sure a cursor secret is configured.
_tmpdir = tempfile.mkdtemp(prefix="ledger-page-test-")
os.environ.setdefault("LEDGER_DB_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("LEADERBOARD_CURSOR_SECRET", "page-test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _post(event_id, participant_id, points):
    r = client.post("/events", json={
        "event_id": event_id, "participant_id": participant_id, "points": points,
    })
    assert r.status_code == 201
    return r.json()["revision"]


def _full_entries(revision):
    return client.get(f"/leaderboard?revision={revision}").json()["entries"]


def _walk_pages(limit, first_params=None):
    """Fetch page 1 with ``first_params``, then follow next_cursor to the end."""
    pages, entries = [], []
    params = dict(first_params or {})
    params["limit"] = limit
    while True:
        r = client.get("/leaderboard/page", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body)
        entries.extend(body["entries"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages, entries
        params = {"limit": limit, "cursor": cursor}


def _valid_cursor():
    """A genuine next_cursor issued by the API."""
    uid = _uid()
    _post(f"cur-{uid}-e1", f"cur-{uid}-a", 3)
    _post(f"cur-{uid}-e2", f"cur-{uid}-b", 2)
    r = client.get("/leaderboard/page", params={"limit": 1})
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor is not None
    return cursor


def _sign(payload_b64: str) -> str:
    secret = os.environ["LEADERBOARD_CURSOR_SECRET"].encode()
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def test_pagination_walk_matches_full_leaderboard_with_ties():
    uid = _uid()
    for i, pts in enumerate([10, 10, 10, 5, 5, 1]):  # ties on total points
        _post(f"pg-{uid}-e{i}", f"pg-{uid}-p{i}", pts)
    rev = client.get("/leaderboard").json()["revision"]
    full = _full_entries(rev)

    for limit in (1, 2, 3, 500):
        pages, entries = _walk_pages(limit, {"revision": rev})
        assert entries == full
        assert all(p["revision"] == rev for p in pages)
        assert pages[-1]["next_cursor"] is None
        for p in pages[:-1]:
            assert len(p["entries"]) == limit
        assert 1 <= len(pages[-1]["entries"]) <= limit
        # ranks run contiguously across page boundaries
        ranks = [e["rank"] for e in entries]
        assert ranks == list(range(1, len(ranks) + 1))


def test_cursor_pins_snapshot_across_writes():
    uid = _uid()
    for i in range(4):
        _post(f"pin-{uid}-e{i}", f"pin-{uid}-p{i}", 10)

    current = client.get("/leaderboard").json()["revision"]
    r1 = client.get("/leaderboard/page", params={"limit": 2})
    assert r1.status_code == 200
    page1 = r1.json()
    assert page1["revision"] == current  # default: latest revision
    cursor = page1["next_cursor"]
    assert cursor is not None

    # Writes after page 1 advance the revision and change totals.
    _post(f"pin-{uid}-e9", f"pin-{uid}-p0", 999)
    assert client.get("/leaderboard").json()["revision"] > current

    # The cursor still reads its own pinned snapshot.
    r2 = client.get("/leaderboard/page", params={"limit": 2, "cursor": cursor})
    assert r2.status_code == 200
    page2 = r2.json()
    assert page2["revision"] == current
    full = _full_entries(current)
    assert page1["entries"] + page2["entries"] == full[:4]


def test_first_page_explicit_revision_pins_snapshot():
    uid = _uid()
    _post(f"snap-{uid}-e1", f"snap-{uid}-a", 5)
    rev = client.get("/leaderboard").json()["revision"]
    _post(f"snap-{uid}-e2", f"snap-{uid}-b", 100)  # advances the revision

    r = client.get("/leaderboard/page", params={"revision": rev, "limit": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == rev
    assert body["entries"] == _full_entries(rev)
    assert body["next_cursor"] is None


def test_tampered_cursor_returns_422():
    cursor = _valid_cursor()
    payload_b64, sig_b64 = cursor.split(".")

    # flip one character in the payload
    i = 5
    repl = "A" if payload_b64[i] != "A" else "B"
    bad_payload = payload_b64[:i] + repl + payload_b64[i + 1:]
    r = client.get("/leaderboard/page", params={"cursor": f"{bad_payload}.{sig_b64}"})
    assert r.status_code == 422

    # flip one character in the signature
    repl = "A" if sig_b64[0] != "A" else "B"
    bad_sig = repl + sig_b64[1:]
    r = client.get("/leaderboard/page", params={"cursor": f"{payload_b64}.{bad_sig}"})
    assert r.status_code == 422


def test_malformed_cursor_returns_422():
    for bad in ("not-a-cursor", "a.b.c", "!!!.###", "", "中文.c2ln"):
        r = client.get("/leaderboard/page", params={"cursor": bad})
        assert r.status_code == 422, bad


def test_valid_signature_but_bad_payload_returns_422():
    # payload is not JSON
    p = base64.urlsafe_b64encode(b"not json").decode()
    r = client.get("/leaderboard/page", params={"cursor": f"{p}.{_sign(p)}"})
    assert r.status_code == 422

    # payload is JSON but misses required fields
    p = base64.urlsafe_b64encode(json.dumps({"v": 1, "revision": 1}).encode()).decode()
    r = client.get("/leaderboard/page", params={"cursor": f"{p}.{_sign(p)}"})
    assert r.status_code == 422

    # unsupported cursor version
    p = base64.urlsafe_b64encode(json.dumps({
        "v": 2, "revision": 1, "total_points": 1, "participant_id": "x",
    }).encode()).decode()
    r = client.get("/leaderboard/page", params={"cursor": f"{p}.{_sign(p)}"})
    assert r.status_code == 422


def test_cursor_conflicting_explicit_revision_returns_422():
    cursor = _valid_cursor()
    payload = json.loads(base64.urlsafe_b64decode(cursor.split(".")[0]))
    rev = payload["revision"]

    ok = client.get("/leaderboard/page", params={"cursor": cursor, "revision": rev})
    assert ok.status_code == 200
    assert ok.json()["revision"] == rev

    conflict = client.get(
        "/leaderboard/page", params={"cursor": cursor, "revision": rev + 1}
    )
    assert conflict.status_code == 422


def test_cursor_signed_with_other_secret_rejected(monkeypatch):
    cursor = _valid_cursor()
    monkeypatch.setenv("LEADERBOARD_CURSOR_SECRET", "some-other-secret")
    r = client.get("/leaderboard/page", params={"cursor": cursor})
    assert r.status_code == 422


def test_limit_validation():
    assert client.get("/leaderboard/page", params={"limit": 0}).status_code == 422
    assert client.get("/leaderboard/page", params={"limit": -1}).status_code == 422
    assert client.get("/leaderboard/page", params={"limit": 501}).status_code == 422
    assert client.get("/leaderboard/page", params={"limit": "abc"}).status_code == 422


def test_unknown_revision_returns_404():
    assert client.get("/leaderboard/page", params={"revision": 0}).status_code == 404
    assert client.get("/leaderboard/page", params={"revision": 999999}).status_code == 404


def test_empty_leaderboard_returns_empty_page(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.db"))
    db.init_db()

    r = client.get("/leaderboard/page")
    assert r.status_code == 200
    assert r.json() == {"revision": 0, "entries": [], "next_cursor": None}

    r = client.get("/leaderboard/page", params={"limit": 10})
    assert r.status_code == 200
    assert r.json() == {"revision": 0, "entries": [], "next_cursor": None}

    # explicit revision on an empty ledger does not exist
    assert client.get("/leaderboard/page", params={"revision": 1}).status_code == 404
