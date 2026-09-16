# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one bounded rolling-90-day Devon live smoke."""

from __future__ import annotations

from datetime import timedelta

from _smoke_captured import run_discovery_smoke

from yimby.authorities.devon import DEVON_PACKAGE
from yimby.portal_time import england_calendar_date

if __name__ == "__main__":
    today = england_calendar_date()
    raise SystemExit(
        run_discovery_smoke("devon", DEVON_PACKAGE, today - timedelta(days=89), today)
    )
