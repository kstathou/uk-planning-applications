# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Camden's Cloudflare-cleared visible-browser transport boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import pytest
from pydantic import HttpUrl

from yimby.authorities.camden.browser_session import (
    CamdenBrowserPayload,
    CamdenBrowserPortalSession,
)
from yimby.domain import TransportMode
from yimby.http_transport import HostRateLimiter
from yimby.transport import (
    AttachmentBodyBlockedError,
    FormField,
    PortalRequest,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from yimby.authorities.camden.browser_session import CamdenBrowserBoundary


class _Boundary:
    def __init__(self, payload: CamdenBrowserPayload) -> None:
        self.payload = payload
        self.requests: list[PortalRequest] = []
        self.closed = False

    async def request(self, request: PortalRequest) -> CamdenBrowserPayload:
        self.requests.append(request)
        return self.payload

    async def aclose(self) -> None:
        self.closed = True


def _factory(
    boundary: CamdenBrowserBoundary,
) -> Callable[[], Awaitable[CamdenBrowserBoundary]]:
    async def create() -> CamdenBrowserBoundary:
        return boundary

    return create


def _session(boundary: CamdenBrowserBoundary) -> CamdenBrowserPortalSession:
    return CamdenBrowserPortalSession(
        boundary_factory=_factory(boundary),
        limiter=HostRateLimiter(minimum_gap=0),
    )


def _request(
    url: str = "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx",
    *,
    method: RequestMethod = RequestMethod.GET,
) -> PortalRequest:
    return PortalRequest(
        url=HttpUrl(url),
        method=method,
        form=(FormField(name="repeated", value="one"),),
    )


def test_camden_browser_session_is_lazy_counted_and_reusable() -> None:
    payload = CamdenBrowserPayload(
        status=200,
        final_url=HttpUrl(
            "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx"
        ),
        body=b"<form id='M3Form'></form>",
        media_type="text/html",
    )
    boundary = _Boundary(payload)
    session = _session(boundary)

    first = asyncio.run(session.fetch(_request()))
    second = asyncio.run(session.fetch(_request(method=RequestMethod.POST)))
    asyncio.run(session.aclose())

    assert first.body == payload.body
    assert second.digest == first.digest
    assert len(boundary.requests) == 2
    assert session.requested_urls == (
        "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx",
        "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx",
    )
    assert session.transferred_bytes == 2 * len(payload.body)
    assert session.attachment_body_requests == 0
    assert session.browser_time_ms >= 0
    assert session.mode == TransportMode.BROWSER
    assert boundary.closed


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            CamdenBrowserPayload(
                status=403,
                final_url=HttpUrl(
                    "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx"
                ),
                body=b"challenge",
                media_type="text/html",
            ),
            SourceUnavailableError,
        ),
        (
            CamdenBrowserPayload(
                status=200,
                final_url=HttpUrl("https://attacker.example/result"),
                body=b"wrong origin",
                media_type="text/html",
            ),
            SourceUnavailableError,
        ),
        (
            CamdenBrowserPayload(
                status=200,
                final_url=HttpUrl(
                    "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/Generic/StdDetails.aspx"
                ),
                body=b"attachment",
                media_type="application/pdf",
            ),
            AttachmentBodyBlockedError,
        ),
    ],
)
def test_camden_browser_session_fails_closed_on_response(
    payload: CamdenBrowserPayload,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        asyncio.run(_session(_Boundary(payload)).fetch(_request()))


def test_camden_browser_session_blocks_attachment_paths_before_browser_io() -> None:
    boundary = _Boundary(
        CamdenBrowserPayload(
            status=200,
            final_url=HttpUrl("https://camdocs.camden.gov.uk/file.pdf"),
            body=b"unused",
            media_type="text/html",
        )
    )
    session = _session(boundary)

    with pytest.raises(AttachmentBodyBlockedError):
        asyncio.run(session.fetch(_request("https://camdocs.camden.gov.uk/file.pdf")))

    assert boundary.requests == []
    assert session.attachment_body_requests == 1
    asyncio.run(session.aclose())
