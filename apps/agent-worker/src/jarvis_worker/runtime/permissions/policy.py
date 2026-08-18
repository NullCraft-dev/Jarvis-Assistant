"""Host-owned lifetime policy for durable permission requests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

DEFAULT_PERMISSION_REQUEST_TTL_SECONDS = 15 * 60
MIN_PERMISSION_REQUEST_TTL_SECONDS = 30
MAX_PERMISSION_REQUEST_TTL_SECONDS = 24 * 60 * 60


def permission_request_ttl_seconds() -> int:
    """Return a bounded TTL; malformed configuration falls back safely."""
    raw = os.getenv("JARVIS_PERMISSION_REQUEST_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_PERMISSION_REQUEST_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PERMISSION_REQUEST_TTL_SECONDS
    return max(
        MIN_PERMISSION_REQUEST_TTL_SECONDS,
        min(value, MAX_PERMISSION_REQUEST_TTL_SECONDS),
    )


def permission_request_deadline(
    created_at: datetime,
    *,
    ttl_seconds: int | None = None,
) -> datetime:
    ttl = permission_request_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if ttl < 1:
        raise ValueError("permission request TTL 必须大于 0")
    return created_at + timedelta(seconds=ttl)


def permission_request_is_expired(*, expires_at: datetime | None, now: datetime) -> bool:
    """Missing deadlines fail closed for legacy or malformed records."""
    return expires_at is None or expires_at <= now
