# Copyright (c) 2026 Kostas Stathoulopoulos

"""Visible-Chrome transport for Camden's managed Cloudflare boundary."""

from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import Field, HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest, FrozenModel, TransportMode
from yimby.http_transport import HostRateLimiter
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalRequest,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_ALLOWED_HOSTS = {
    "camdocs.camden.gov.uk",
    "planningrecords.camden.gov.uk",
}
_ATTACHMENT_SUFFIXES = {
    ".bmp",
    ".doc",
    ".docx",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}
_ATTACHMENT_PATH_FRAGMENTS = ("/document/download", "/file/document")
_ATTACHMENT_MEDIA_PREFIXES = (
    "application/msword",
    "application/octet-stream",
    "application/pdf",
    "application/vnd.ms-",
    "application/vnd.openxmlformats-",
    "application/zip",
    "audio/",
    "image/",
    "video/",
)
_BLOCKED_RESOURCE_TYPES = {"image", "media"}
_CHALLENGE_HOST = "planningrecords.camden.gov.uk"
_CHALLENGE_TITLE = "Just a moment..."
_CHALLENGE_TIMEOUT_MS = 60_000
_SUCCESS_MIN = 200
_SUCCESS_MAX = 300
_CHALLENGE_STATUS = 403


class CamdenBrowserPayload(FrozenModel):
    """One top-level browser response returned without session secrets."""

    status: int = Field(ge=100, le=599)
    final_url: HttpUrl
    body: bytes
    media_type: str
    content_disposition: str = ""


class CamdenBrowserBoundary(Protocol):
    """Injectable browser lifecycle used by the Camden portal session."""

    async def request(self, request: PortalRequest) -> CamdenBrowserPayload:
        """Issue one top-level GET or form POST in the cleared browser."""

    async def aclose(self) -> None:
        """Release browser resources."""


class CamdenChallengeTimeoutError(SourceUnavailableError):
    """Camden's managed browser challenge did not clear in time."""

    def __init__(self) -> None:
        """Report a stable, sanitised managed-challenge timeout."""
        super().__init__("Camden browser challenge did not clear within 60 seconds")


class CamdenVisibleChromeBoundary:
    """One visible Chrome page that owns Cloudflare clearance and form navigation."""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
    ) -> None:
        """Retain the browser lifecycle and its cleared cookie context."""
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._pages = {_CHALLENGE_HOST: page}

    @classmethod
    async def create(cls) -> CamdenVisibleChromeBoundary:
        """Launch visible stable Chrome so the managed challenge can execute."""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(accept_downloads=False)
        page = await context.new_page()
        boundary = cls(playwright, browser, context, page)
        await context.route("**/*", boundary._route_request)
        return boundary

    async def _route_request(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        should_block = (
            _is_attachment_path(path)
            or request.resource_type in _BLOCKED_RESOURCE_TYPES
        )
        if should_block:
            await route.abort()
        else:
            await route.continue_()

    async def request(self, request: PortalRequest) -> CamdenBrowserPayload:
        """Navigate or submit an ordered hidden-field form and capture HTML."""
        raw_url = str(request.url)
        page = await self._page_for(raw_url)
        if request.method == RequestMethod.POST:
            response = await self._submit_form(page, request)
        else:
            response = await page.goto(raw_url, wait_until="domcontentloaded")
        if response is None:
            raise _source_error(raw_url, "missing browser response")
        headers = await response.all_headers()
        status = response.status
        if (
            status == _CHALLENGE_STATUS
            and headers.get("cf-mitigated", "").casefold() == "challenge"
            and urlsplit(raw_url).hostname == _CHALLENGE_HOST
        ):
            try:
                await page.wait_for_function(
                    f"document.title !== {_CHALLENGE_TITLE!r}",
                    timeout=_CHALLENGE_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as error:
                raise CamdenChallengeTimeoutError from error
            await page.wait_for_load_state("domcontentloaded")
            status = 200
        body = (await page.content()).encode()
        return CamdenBrowserPayload(
            status=status,
            final_url=HttpUrl(page.url),
            body=body,
            media_type=headers.get("content-type", "text/html")
            .partition(";")[0]
            .strip()
            .lower(),
            content_disposition=headers.get("content-disposition", ""),
        )

    async def _page_for(self, url: str) -> Page:
        host = urlsplit(url).hostname or ""
        page = self._pages.get(host)
        if page is None:
            page = await self._context.new_page()
            self._pages[host] = page
        return page

    async def _submit_form(
        self,
        page: Page,
        request: PortalRequest,
    ) -> Response | None:
        fields = tuple(
            {"name": field.name, "value": field.value} for field in request.form
        )
        async with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            await page.evaluate(
                """
                ({url, fields}) => {
                  const form = document.createElement("form");
                  form.method = "POST";
                  form.action = url;
                  for (const field of fields) {
                    const input = document.createElement("input");
                    input.type = "hidden";
                    input.name = field.name;
                    input.value = field.value;
                    form.appendChild(input);
                  }
                  document.body.appendChild(form);
                  form.submit();
                }
                """,
                {"url": str(request.url), "fields": fields},
            )
        return await navigation.value

    async def aclose(self) -> None:
        """Close page, context, browser, and Playwright in ownership order."""
        for page in self._pages.values():
            await page.close()
        await self._context.close()
        await self._browser.close()
        await self._playwright.stop()


class CamdenBrowserPortalSession:
    """Rate-limited Camden session backed by one lazily created Chrome boundary."""

    def __init__(
        self,
        *,
        boundary_factory: Callable[[], Awaitable[CamdenBrowserBoundary]] = (
            CamdenVisibleChromeBoundary.create
        ),
        limiter: HostRateLimiter | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Configure a lazy browser and the shared two-second host limiter."""
        self._boundary_factory = boundary_factory
        self._boundary: CamdenBrowserBoundary | None = None
        self._limiter = limiter or HostRateLimiter()
        self._clock = clock
        self._requested_urls: list[str] = []
        self._attachment_body_requests = 0
        self._transferred_bytes = 0
        self._browser_time_ms = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Retrieve one allowed HTML response and retain its digestable body."""
        raw_url = str(request.url)
        parts = urlsplit(raw_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in _ALLOWED_HOSTS
            or _is_attachment_path(parts.path)
        ):
            if _is_attachment_path(parts.path):
                self._attachment_body_requests += 1
                raise _attachment_error(parts.hostname)
            raise _source_error(raw_url, "request origin is not Camden")
        boundary = await self._get_boundary()
        started = self._clock()
        try:
            async with self._limiter.turn(parts.hostname):
                payload = await boundary.request(request)
        finally:
            self._browser_time_ms += max(
                0,
                round((self._clock() - started) * 1000),
            )
        final = urlsplit(str(payload.final_url))
        if (
            payload.status < _SUCCESS_MIN
            or payload.status >= _SUCCESS_MAX
            or final.scheme != "https"
            or final.hostname not in _ALLOWED_HOSTS
        ):
            raise _source_error(raw_url, f"HTTP {payload.status}")
        if _is_attachment_payload(payload):
            self._attachment_body_requests += 1
            raise _attachment_error(final.hostname)
        safe_url = _safe_url(raw_url)
        self._requested_urls.append(safe_url)
        self._transferred_bytes += len(payload.body)
        return EvidenceCapture(
            url=HttpUrl(safe_url),
            media_type=payload.media_type,
            body=payload.body,
            digest=EvidenceDigest(sha256(payload.body).hexdigest()),
        )

    async def _get_boundary(self) -> CamdenBrowserBoundary:
        if self._boundary is None:
            self._boundary = await self._boundary_factory()
        return self._boundary

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Return successfully retrieved top-level URLs without query strings."""
        return tuple(self._requested_urls)

    @property
    def attachment_body_requests(self) -> int:
        """Return blocked attachment attempts."""
        return self._attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        """Return retained top-level HTML bytes."""
        return self._transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        """Return time spent waiting on rate-limited browser operations."""
        return self._browser_time_ms

    @property
    def mode(self) -> TransportMode:
        """Identify the session as live browser transport."""
        return TransportMode.BROWSER

    async def aclose(self) -> None:
        """Close the browser if this lazy session opened one."""
        if self._boundary is not None:
            await self._boundary.aclose()


def _is_attachment_path(path: str) -> bool:
    lowered = path.casefold()
    return PurePosixPath(path).suffix.lower() in _ATTACHMENT_SUFFIXES or any(
        fragment in lowered for fragment in _ATTACHMENT_PATH_FRAGMENTS
    )


def _is_attachment_payload(payload: CamdenBrowserPayload) -> bool:
    disposition = payload.content_disposition.casefold()
    media_type = payload.media_type.casefold()
    return (
        "attachment" in disposition
        or "filename=" in disposition
        or media_type.startswith(_ATTACHMENT_MEDIA_PREFIXES)
    )


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _source_error(url: str, reason: str) -> SourceUnavailableError:
    parts = urlsplit(url)
    safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return SourceUnavailableError(
        f"Camden browser source unavailable: {safe_url} {reason}"
    )


def _attachment_error(host: str | None) -> AttachmentBodyBlockedError:
    return AttachmentBodyBlockedError(
        f"attachment body blocked for {host or 'unknown host'}"
    )
