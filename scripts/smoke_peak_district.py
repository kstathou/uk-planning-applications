# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one bounded rolling-week Peak District live smoke."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _smoke_captured import run_discovery_smoke

from yimby.authorities.peak_district import PEAK_DISTRICT_PACKAGE

if __name__ == "__main__":
    today = datetime.now(UTC).date()
    raise SystemExit(
        run_discovery_smoke(
            "peak-district",
            PEAK_DISTRICT_PACKAGE,
            today - timedelta(days=6),
            today,
        )
    )
