# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one bounded one-day Arun live smoke."""

from __future__ import annotations

from datetime import UTC, datetime

from _smoke_captured import run_discovery_smoke

from yimby.authorities.arun import ARUN_PACKAGE

if __name__ == "__main__":
    today = datetime.now(UTC).date()
    raise SystemExit(run_discovery_smoke("arun", ARUN_PACKAGE, today, today))
