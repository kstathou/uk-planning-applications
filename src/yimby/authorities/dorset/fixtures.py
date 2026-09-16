# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Dorset response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import BASE_URL

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "P/FUL/2026/01001"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Dorset session."""
    searches = tuple(
        (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&offset={offset}"
        )
        for offset in ("0", "complete")
    )
    detail = f"{BASE_URL}/application/{quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
