# Copyright (c) 2026 Kostas Stathoulopoulos

"""Small operational helper for authority-owned fixture builders."""

from __future__ import annotations

from importlib.resources import files

from yimby.transport import FixtureResponse, FixtureSession


def build_fixture_session(
    package: str,
    *,
    search_urls: tuple[str, ...],
    detail_url: str,
) -> FixtureSession:
    """Load one authority's sanitised search and detail captures."""
    fixture_root = files(package).joinpath("fixtures")
    search = FixtureResponse(body=fixture_root.joinpath("search.html").read_bytes())
    detail = FixtureResponse(body=fixture_root.joinpath("detail.html").read_bytes())
    responses = dict.fromkeys(search_urls, search)
    responses[detail_url] = detail
    return FixtureSession(responses)
