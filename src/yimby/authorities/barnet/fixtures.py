# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitised Barnet response fixtures."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from yimby.transport import FixtureResponse, FixtureSession

from .adapter import BASE_URL

if TYPE_CHECKING:
    from yimby.domain import DiscoveryWindow


def fixture_session(
    window: DiscoveryWindow,
    *,
    comments_available: bool = True,
    comments_html: bytes | None = None,
) -> FixtureSession:
    """Build a network-free Barnet portal session."""
    fixture_root = files("yimby.authorities.barnet").joinpath("fixtures")
    search = fixture_root.joinpath("search.html").read_bytes()
    detail = fixture_root.joinpath("detail.html").read_bytes()
    responses = {
        (
            f"{BASE_URL}/search?start={window.start.isoformat()}"
            f"&end={window.end.isoformat()}&cursor={cursor}"
        ): FixtureResponse(body=search)
        for cursor in ("start", "complete")
    }
    responses[f"{BASE_URL}/application/23/0001"] = FixtureResponse(body=detail)
    if comments_available:
        comments = (
            fixture_root.joinpath("comments.html").read_bytes()
            if comments_html is None
            else comments_html
        )
        responses[f"{BASE_URL}/application/23/0001/comments"] = FixtureResponse(
            body=comments
        )
    return FixtureSession(responses)
