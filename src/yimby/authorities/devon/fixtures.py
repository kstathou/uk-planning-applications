# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Devon response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import BASE_URL

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "DCC/4473/2026"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Devon session."""
    searches = tuple(
        (
            f"{BASE_URL}/Search/Results?receivedFrom={window.start.isoformat()}"
            f"&receivedTo={window.end.isoformat()}&page={page}"
        )
        for page in ("1", "complete")
    )
    detail = f"{BASE_URL}/Application/Detail?ref={quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
