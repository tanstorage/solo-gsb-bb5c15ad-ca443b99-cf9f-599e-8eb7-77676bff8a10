"""HMAC-signed cursors for ``GET /leaderboard/page``.

Token format: ``<payload_b64>.<sig_b64>`` where the payload is compact JSON
``{"v":1,"revision":R,"total_points":T,"participant_id":P}`` (base64url) and
the signature is the base64url HMAC-SHA256 of the payload, keyed by the
``LEADERBOARD_CURSOR_SECRET`` environment variable.

Any deviation — bad shape, bad signature, undecodable payload, unsupported
version — is reported to the API layer as HTTP 422.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

CURSOR_VERSION = 1

# Development fallback so local runs and tests work out of the box.
# Production deployments must set LEADERBOARD_CURSOR_SECRET explicitly.
DEFAULT_SECRET = "insecure-dev-cursor-secret"


class CursorPayload(BaseModel):
    """Sort-key bookmark bound to a pinned leaderboard revision."""

    model_config = ConfigDict(strict=True)

    v: int
    revision: int = Field(ge=1)
    total_points: int
    participant_id: str


def _secret() -> bytes:
    return os.environ.get("LEADERBOARD_CURSOR_SECRET", DEFAULT_SECRET).encode("utf-8")


def _b64decode(part: str) -> bytes:
    return base64.b64decode(part.encode("ascii"), altchars=b"-_", validate=True)


def _invalid_cursor() -> HTTPException:
    return HTTPException(status_code=422, detail="invalid cursor")


def encode_cursor(revision: int, total_points: int, participant_id: str) -> str:
    payload = CursorPayload(
        v=CURSOR_VERSION,
        revision=revision,
        total_points=total_points,
        participant_id=participant_id,
    )
    payload_b64 = base64.urlsafe_b64encode(
        payload.model_dump_json().encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{base64.urlsafe_b64encode(sig).decode('ascii')}"


def decode_cursor(token: str) -> CursorPayload:
    """Verify and decode a cursor token; any failure raises HTTP 422."""
    try:
        payload_b64, sig_b64 = token.split(".")
        provided_sig = _b64decode(sig_b64)
        expected_sig = hmac.new(
            _secret(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected_sig, provided_sig):
            raise ValueError("signature mismatch")
        payload = CursorPayload.model_validate_json(_b64decode(payload_b64))
        if payload.v != CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
    except Exception:
        raise _invalid_cursor() from None
    return payload
