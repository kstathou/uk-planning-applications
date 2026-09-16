# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Camden response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import DETAIL_BASE, SEARCH_BASE

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "2026/2706/L"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Camden session."""
    searches = tuple(
        (
            f"{SEARCH_BASE}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&view={cursor}"
        )
        for cursor in ("initial", "complete")
    )
    detail = f"{DETAIL_BASE}/application?reference={quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
