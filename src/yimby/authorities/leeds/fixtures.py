# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Leeds response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import BASE_URL

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "26/01234/FU"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Leeds session."""
    searches = tuple(
        (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&page={page}"
        )
        for page in ("1", "complete")
    )
    detail = f"{BASE_URL}/application/{quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
