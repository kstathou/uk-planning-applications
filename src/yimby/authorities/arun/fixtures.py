# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Arun response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import BASE_URL

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "BR/156/25/PL"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Arun session."""
    searches = tuple(
        (
            f"{BASE_URL}/Search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&row={row}"
        )
        for row in ("first", "complete")
    )
    detail = f"{BASE_URL}/PlanningDetails?reference={quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
