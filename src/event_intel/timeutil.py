"""UTC-aware timestamp helpers (Phase 18V, review round-2 #1).

Brave's `page_age`/`age` can be a date-only or tz-less ISO string, which
`datetime.fromisoformat` parses to a NAIVE datetime. Mixing that with a UTC-aware
`reference_date` in recency math raises `TypeError: can't subtract offset-naive
and offset-aware datetimes`. Normalize everything to UTC at every boundary
(search parse + on-disk cache restore + scoring) so the rest of the code only
ever sees aware UTC datetimes — or None.

Pure stdlib — safe to import from cold provider/scoring modules.
"""
from __future__ import annotations

from datetime import datetime, timezone


def normalize_utc(dt: datetime | None) -> datetime | None:
    """Naive → assume UTC; aware → convert to UTC; None → None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_utc(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime. None on any failure
    (published_at is advisory — never fail a pipeline over a bad timestamp)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return normalize_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def recency_weight(
    published_at: datetime | str | None,
    *,
    reference_date: datetime,
    half_life_days: float,
) -> float:
    """Exponential-decay recency weight in [0, 1].

    - None / unparseable → 0.0 (no recency signal, but the news still counts).
    - future (published_at > reference_date) → 0.0 (clock skew / bad data).
    - else 0.5 ** (age_days / half_life_days); half_life <= 0 → 1.0 (decay off).
    """
    dt = published_at if isinstance(published_at, datetime) else parse_iso_utc(published_at)
    dt = normalize_utc(dt)
    if dt is None:
        return 0.0
    ref = normalize_utc(reference_date)
    age_days = (ref - dt).total_seconds() / 86400.0
    if age_days < 0:
        return 0.0
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)
