# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one explicitly authorised, bounded Durham live smoke."""

from __future__ import annotations

from _smoke_idox import run_smoke

from yimby.authorities.durham import DURHAM_PACKAGE

if __name__ == "__main__":
    raise SystemExit(run_smoke("durham", DURHAM_PACKAGE))
