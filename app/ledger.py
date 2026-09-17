"""Core ledger logic: idempotent immutable writes + revisioned reads."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException

from .db import read_tx, write_tx
from .schemas import (
    EffectiveEvent,
    EventIn,
    EventOut,
    LeaderboardEntry,
    LeaderboardOut,
)


def _canonical_payload(ev: EventIn) -> str:
    """Canonical JSON of the client-supplied payload (key order independent)."""
    return json.dumps(ev.model_dump(), sort_keys=True, separators=(",", ":"))


def _row_to_event(row: sqlite3.Row) -> EventOut:
    return EventOut(
        event_id=row["event_id"],
        participant_id=row["participant_id"],
        points=row["points"],
        correction_of=row["correction_of"],
        revision=row["revision"],
        created_at=row["created_at"],
    )


def create_event(ev: EventIn) -> Tuple[EventOut, bool]:
    """Insert an immutable event.

    Returns (event, created). If ``event_id`` already exists:
      * identical payload  -> replay: return the stored event, created=False
      * different payload  -> 409 Conflict
    """
    if ev.correction_of is not None and ev.correction_of == ev.event_id:
        raise HTTPException(
            status_code=422, detail="correction_of must not equal event_id"
        )

    payload = _canonical_payload(ev)

    with write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] == payload:
                return _row_to_event(row), False
            raise HTTPException(
                status_code=409,
                detail=f"event_id '{ev.event_id}' already exists with a different payload",
            )

        if ev.correction_of is not None:
            target = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (ev.correction_of,)
            ).fetchone()
            if target is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"correction_of references unknown event '{ev.correction_of}'",
                )

        revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM events"
        ).fetchone()[0]
        created_at = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        conn.execute(
            "INSERT INTO events (event_id, participant_id, points, correction_of, payload, revision, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_id,
                ev.participant_id,
                ev.points,
                ev.correction_of,
                payload,
                revision,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        return _row_to_event(row), True


def get_event(event_id: str) -> EventOut:
    with read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"event '{event_id}' not found")
    return _row_to_event(row)


def list_events() -> list[EventOut]:
    with read_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY revision ASC"
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def current_revision() -> int:
    with read_tx() as conn:
        return conn.execute("SELECT COALESCE(MAX(revision), 0) FROM events").fetchone()[0]


def leaderboard(revision: Optional[int] = None) -> LeaderboardOut:
    """Leaderboard as of ``revision`` (default: latest).

    An event is *effective* at revision R iff it exists at R (revision <= R)
    and no event with revision <= R corrects it. Hence, for every correction
    chain, only the chain tip that existed at R is counted.
    """
    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]

        if revision is None:
            revision = max_rev
        elif revision < 1 or revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {revision} not found"
            )

        rows = conn.execute(
            """
            SELECT e.event_id, e.participant_id, e.points, e.revision, e.correction_of
            FROM events e
            WHERE e.revision <= ?
              AND NOT EXISTS (
                    SELECT 1 FROM events c
                    WHERE c.correction_of = e.event_id
                      AND c.revision <= ?
              )
            ORDER BY e.participant_id ASC, e.revision ASC
            """,
            (revision, revision),
        ).fetchall()

    totals: dict[str, int] = {}
    details: dict[str, list[EffectiveEvent]] = {}
    for r in rows:
        pid = r["participant_id"]
        totals[pid] = totals.get(pid, 0) + r["points"]
        details.setdefault(pid, []).append(
            EffectiveEvent(
                event_id=r["event_id"],
                points=r["points"],
                revision=r["revision"],
                correction_of=r["correction_of"],
            )
        )

    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    entries = [
        LeaderboardEntry(
            rank=i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(ordered)
    ]
    return LeaderboardOut(revision=revision, entries=entries)
