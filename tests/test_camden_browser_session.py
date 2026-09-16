# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004

"""Camden's Cloudflare-cleared visible-browser transport boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import HttpUrl

from yimby.authorities.camden import browser_session
from yimby.authorities.camden.browser_session import (
    CamdenBrowserPayload,
    CamdenBrowserPortalSession,
    CamdenChallengeTimeoutError,
    CamdenVisibleChromeBoundary,
)
from yimby.domain import TransportMode
from yimby.http_transport import HostRateLimiter
from yimby.transport import (
    AttachmentBodyBlockedError,
    FormField,
    PortalRequest,
    RequestIntent,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Self

    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response

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
        intent=RequestIntent.SEARCH,
        method=method,
        form=(
            (FormField(name="repeated", value="one"),)
            if method == RequestMethod.POST
            else ()
        ),
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
    assert session.attempted_request_count == 2
    assert session.successful_capture_count == 2
    assert session.retained_html_bytes == 2 * len(payload.body)
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
    session = _session(_Boundary(payload))
    with pytest.raises(error):
        asyncio.run(session.fetch(_request()))
    assert session.attempted_request_count == 1
    assert session.successful_capture_count == 0
    assert session.retained_html_bytes == 0


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


def test_camden_browser_session_rejects_non_camden_request_origin() -> None:
    boundary = _Boundary(
        CamdenBrowserPayload(
            status=200,
            final_url=HttpUrl("https://planningrecords.camden.gov.uk/result"),
            body=b"unused",
            media_type="text/html",
        )
    )
    with pytest.raises(SourceUnavailableError, match="origin is not Camden"):
        asyncio.run(
            _session(boundary).fetch(_request("https://attacker.example/result"))
        )
    assert boundary.requests == []


class _Navigation:
    def __init__(self, response: Response | None) -> None:
        self.response = response

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        return None

    @property
    def value(self) -> Awaitable[Response | None]:
        async def result() -> Response | None:
            return self.response

        return result()


def _route(
    url: str,
    *,
    resource_type: str = "document",
) -> MagicMock:
    route = MagicMock()
    route.request.url = url
    route.request.resource_type = resource_type
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    return route


def test_camden_visible_chrome_launches_routes_submits_and_closes(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = MagicMock()
    challenge.status = 403
    challenge.all_headers = AsyncMock(
        return_value={
            "content-type": "text/html; charset=utf-8",
            "cf-mitigated": "challenge",
        }
    )
    success = MagicMock()
    success.status = 200
    success.all_headers = AsyncMock(
        return_value={
            "content-type": "text/html; charset=utf-8",
            "content-disposition": "inline",
        }
    )
    page = MagicMock()
    success.request.is_navigation_request.return_value = True
    success.frame = page.main_frame
    page.url = "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx"
    page.goto = AsyncMock(return_value=challenge)
    page.content = AsyncMock(return_value='<form id="M3Form"></form>')
    response_listeners: list[Callable[[Response], None]] = []
    page.on.side_effect = lambda _event, listener: response_listeners.append(listener)

    def clear_challenge(*_args: object, **_kwargs: object) -> None:
        subresource = MagicMock()
        subresource.request.is_navigation_request.return_value = False
        wrong_frame = MagicMock()
        wrong_frame.request.is_navigation_request.return_value = True
        wrong_frame.frame = MagicMock()
        response_listeners[-1](cast("Response", subresource))
        response_listeners[-1](cast("Response", wrong_frame))
        response_listeners[-1](cast("Response", success))

    page.wait_for_function = AsyncMock(side_effect=clear_challenge)
    page.wait_for_load_state = AsyncMock()
    page.evaluate = AsyncMock()
    page.expect_navigation.return_value = _Navigation(cast("Response", success))
    page.close = AsyncMock()
    documents_page = MagicMock()
    documents_page.url = "https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec"
    documents_page.goto = AsyncMock(return_value=success)
    documents_page.content = AsyncMock(return_value="<html>documents</html>")
    documents_page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(side_effect=(page, documents_page))
    context.route = AsyncMock()
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)
    playwright = MagicMock()
    playwright.chromium = chromium
    playwright.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr(browser_session, "async_playwright", lambda: starter)

    async def exercise() -> None:
        boundary = await CamdenVisibleChromeBoundary.create()
        route_handler = context.route.await_args.args[1]

        allowed = _route("https://planningrecords.camden.gov.uk/style.css")
        await route_handler(allowed)
        allowed.continue_.assert_awaited_once()

        attachment = _route("https://camdocs.camden.gov.uk/file.pdf")
        await route_handler(attachment)
        attachment.abort.assert_awaited_once()

        image = _route(
            "https://planningrecords.camden.gov.uk/logo", resource_type="image"
        )
        await route_handler(image)
        image.abort.assert_awaited_once()

        get_payload = await boundary.request(_request())
        assert get_payload.status == 200
        assert get_payload.media_type == "text/html"
        page.wait_for_function.assert_awaited_once()

        documents_payload = await boundary.request(
            _request("https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec")
        )
        assert documents_payload.body == b"<html>documents</html>"

        challenge_script = _route("https://planningrecords.camden.gov.uk/script.js")
        await route_handler(challenge_script)
        challenge_script.continue_.assert_awaited_once()
        navigation = _route("https://planningrecords.camden.gov.uk/result")
        await route_handler(navigation)
        navigation.continue_.assert_awaited_once()

        page.content.return_value = "<html>result</html>"
        post_payload = await boundary.request(_request(method=RequestMethod.POST))
        assert post_payload.content_disposition == "inline"
        argument = page.evaluate.await_args.args[1]
        assert argument["fields"] == ({"name": "repeated", "value": "one"},)

        await boundary.aclose()

    asyncio.run(exercise())

    chromium.launch.assert_awaited_once_with(channel="chrome", headless=False)
    browser.new_context.assert_awaited_once_with(accept_downloads=False)
    assert context.new_page.await_count == 2
    context.route.assert_awaited_once()
    page.close.assert_awaited_once()
    documents_page.close.assert_awaited_once()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


def test_camden_visible_chrome_handles_plain_and_missing_responses() -> None:
    response = MagicMock()
    response.status = 200
    response.all_headers = AsyncMock(return_value={})
    page = MagicMock()
    page.url = "https://planningrecords.camden.gov.uk/result"
    page.goto = AsyncMock(return_value=response)
    page.content = AsyncMock(return_value="<form id='M3Form'></form>")
    boundary = CamdenVisibleChromeBoundary(
        cast("Playwright", MagicMock()),
        cast("Browser", MagicMock()),
        cast("BrowserContext", MagicMock()),
        cast("Page", page),
    )
    payload = asyncio.run(boundary.request(_request()))
    assert payload.status == 200

    page.goto.return_value = None
    with pytest.raises(SourceUnavailableError, match="missing browser response"):
        asyncio.run(boundary.request(_request()))

    challenge = MagicMock()
    challenge.status = 403
    challenge.all_headers = AsyncMock(return_value={"cf-mitigated": "challenge"})
    page.goto.return_value = challenge
    page.wait_for_function = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    with pytest.raises(SourceUnavailableError, match="clearance response missing"):
        asyncio.run(boundary.request(_request()))


def test_camden_visible_chrome_names_managed_challenge_timeout() -> None:
    response = MagicMock()
    response.status = 403
    response.all_headers = AsyncMock(return_value={"cf-mitigated": "challenge"})
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.wait_for_function = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
    boundary = CamdenVisibleChromeBoundary(
        cast("Playwright", MagicMock()),
        cast("Browser", MagicMock()),
        cast("BrowserContext", MagicMock()),
        cast("Page", page),
    )

    with pytest.raises(CamdenChallengeTimeoutError, match="within 60 seconds"):
        asyncio.run(boundary.request(_request()))


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (199, {}),
        (500, {}),
        (200, {"cf-mitigated": "challenge"}),
    ],
)
def test_camden_visible_chrome_rejects_invalid_clearance_response(
    status: int,
    headers: dict[str, str],
) -> None:
    challenge = MagicMock()
    challenge.status = 403
    challenge.all_headers = AsyncMock(return_value={"cf-mitigated": "challenge"})
    cleared = MagicMock()
    cleared.status = status
    cleared.all_headers = AsyncMock(return_value=headers)
    page = MagicMock()
    page.url = "https://planningrecords.camden.gov.uk/result"
    cleared.request.is_navigation_request.return_value = True
    cleared.frame = page.main_frame
    listeners: list[Callable[[Response], None]] = []
    page.on.side_effect = lambda _event, listener: listeners.append(listener)
    page.goto = AsyncMock(return_value=challenge)
    page.wait_for_function = AsyncMock(
        side_effect=lambda *_args, **_kwargs: listeners[-1](cast("Response", cleared))
    )
    page.wait_for_load_state = AsyncMock()
    boundary = CamdenVisibleChromeBoundary(
        cast("Playwright", MagicMock()),
        cast("Browser", MagicMock()),
        cast("BrowserContext", MagicMock()),
        cast("Page", page),
    )

    with pytest.raises(SourceUnavailableError, match="clearance returned HTTP"):
        asyncio.run(boundary.request(_request()))
