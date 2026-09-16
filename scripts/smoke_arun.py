# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one bounded one-day Arun live smoke."""

from __future__ import annotations

from _smoke_captured import run_discovery_smoke

from yimby.authorities.arun import ARUN_PACKAGE
from yimby.portal_time import england_calendar_date

if __name__ == "__main__":
    today = england_calendar_date()
    raise SystemExit(run_discovery_smoke("arun", ARUN_PACKAGE, today, today))
