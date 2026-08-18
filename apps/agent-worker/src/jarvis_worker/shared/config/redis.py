"""Shared Redis connection configuration for every Python Runtime process."""

from __future__ import annotations

import os


def redis_db_from_env() -> int:
    """Mirror Gateway's lenient DB parsing so every Runtime process agrees."""
    raw = os.getenv("JARVIS_REDIS_DB", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def redis_password_from_env() -> str:
    """Read the credential without logging or placing it in public DTOs."""
    return os.getenv("JARVIS_REDIS_PASSWORD", "")
