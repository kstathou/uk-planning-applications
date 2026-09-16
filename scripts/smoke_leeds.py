# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run one explicitly authorised, bounded Leeds discovery smoke."""

from __future__ import annotations

from _smoke_idox import run_smoke

from yimby.authorities.leeds import LEEDS_PACKAGE
from yimby.authorities.leeds.adapter import (
    LeedsDetailUnavailableError,
    LeedsDetailUnverifiedError,
)

if __name__ == "__main__":
    raise SystemExit(
        run_smoke(
            "leeds",
            LEEDS_PACKAGE,
            expected_detail_errors=(
                LeedsDetailUnavailableError,
                LeedsDetailUnverifiedError,
            ),
        )
    )
