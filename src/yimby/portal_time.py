# Copyright (c) 2026 Kostas Stathoulopoulos

"""Calendar-date rules for English public planning portals."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_ENGLAND_TIME_ZONE = ZoneInfo("Europe/London")


class NaivePortalTimeError(ValueError):
    """A civil portal date cannot be derived from an unzoned instant."""

    def __init__(self) -> None:
        """Describe the missing timezone without including input data."""
        super().__init__("portal calendar time must be timezone-aware")


def england_calendar_date(instant: datetime | None = None) -> date:
    """Return the civil date used by planning authorities in England."""
    observed = instant or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise NaivePortalTimeError
    return observed.astimezone(_ENGLAND_TIME_ZONE).date()
