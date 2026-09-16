# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one explicitly authorised, bounded West Suffolk live smoke."""

from __future__ import annotations

from _smoke_idox import run_smoke

from yimby.authorities.west_suffolk import WEST_SUFFOLK_PACKAGE

if __name__ == "__main__":
    raise SystemExit(run_smoke("west-suffolk", WEST_SUFFOLK_PACKAGE))
