# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Peak District response fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from yimby.fixture_support import build_fixture_session

from .adapter import ASSURE_BASE, LEGACY_BASE

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

REFERENCE = "NP/DIS/0926/0917"


def fixture_session(window: DiscoveryWindow) -> FixtureSession:
    """Build a network-free Peak District session."""
    searches = tuple(
        (
            f"{LEGACY_BASE}/search?validatedFrom={window.start.isoformat()}"
            f"&validatedTo={window.end.isoformat()}&offset={offset}"
        )
        for offset in ("0", "complete")
    )
    detail = f"{ASSURE_BASE}/Planning/Details/{quote(REFERENCE, safe='')}"
    return build_fixture_session(
        __package__,
        search_urls=searches,
        detail_url=detail,
    )
