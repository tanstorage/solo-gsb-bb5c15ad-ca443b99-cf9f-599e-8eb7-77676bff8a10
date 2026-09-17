from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    """Write model for a points event (immutable once accepted)."""

    event_id: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=128)
    points: int
    correction_of: Optional[str] = Field(
        default=None,
        description="event_id of an existing event this event corrects; "
        "corrections may themselves be corrected (chain).",
    )


class EventOut(BaseModel):
    event_id: str
    participant_id: str
    points: int
    correction_of: Optional[str]
    revision: int
    created_at: str


class EffectiveEvent(BaseModel):
    event_id: str
    points: int
    revision: int
    correction_of: Optional[str]


class LeaderboardEntry(BaseModel):
    rank: int
    participant_id: str
    total_points: int
    effective_events: List[EffectiveEvent]


class LeaderboardOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]


class LeaderboardPageOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]
    next_cursor: Optional[str] = Field(
        default=None,
        description="Signed cursor for the next page; null on the last page.",
    )
