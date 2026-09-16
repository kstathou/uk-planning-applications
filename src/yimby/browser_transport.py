# Copyright (c) 2026 Kostas Stathoulopoulos

"""Narrow one-worker Playwright boundary for future browser-live adapters."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from pydantic import HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest, FrozenModel, TransportMode
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalRequest,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
_ATTACHMENT_PATH_FRAGMENTS = (
    "/document/download",
    "/sfc/servlet.shepherd/document/download/",
    "/sfc/servlet.shepherd/version/download/",
    "/downloadall",
)
_ATTACHMENT_MEDIA_PREFIXES = (
    "audio/",
    "application/msword",
    "application/octet-stream",
    "application/pdf",
    "application/vnd.ms-",
    "application/vnd.openxmlformats-",
    "application/zip",
    "image/",
    "video/",
)
_BLOCKED_RESOURCE_TYPES = {"image", "media"}


class BrowserPayload(FrozenModel):
    """HTML and response metadata returned by the browser boundary."""

    body: bytes
    media_type: str
    content_disposition: str = ""


class BrowserBoundary(Protocol):
    """Small browser surface that deterministic tests can replace."""

    async def open(self, url: str) -> BrowserPayload:
        """Navigate to one HTML page and return its rendered markup."""

    async def aclose(self) -> None:
        """Release browser resources."""


@runtime_checkable
class InteractiveBrowserBoundary(Protocol):
    """Generic page lifecycle used only by authority-owned page objects."""

    async def interact[T](self, operation: Callable[[Page], Awaitable[T]]) -> T:
        """Run one authority-owned interaction in a fresh page."""
        ...


class BrowserWorker:
    """Permit exactly one browser operation at a time."""

    def __init__(self) -> None:
        """Create the single browser semaphore."""
        self._semaphore = asyncio.Semaphore(1)

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run one operation inside the shared browser slot."""
        async with self._semaphore:
            return await operation()


class PlaywrightBoundary:
    """Production Playwright lifecycle and download-blocking boundary."""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
    ) -> None:
        """Retain the Playwright resources as one owned lifecycle."""
        self._playwright = playwright
        self._browser = browser
        self._context = context

    @classmethod
    async def create(cls, *, headless: bool = True) -> PlaywrightBoundary:
        """Launch Chromium with a persistent in-memory cookie context."""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=False)

        async def route_request(route: Route) -> None:
            request = route.request
            path = urlsplit(request.url).path
            if (
                _is_attachment_path(path)
                or request.resource_type in _BLOCKED_RESOURCE_TYPES
            ):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_request)
        return cls(playwright, browser, context)

    async def open(self, url: str) -> BrowserPayload:
        """Render one page while the context blocks download resources."""
        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            if response is None:
                raise _source_error(urlsplit(url).hostname)
            headers = await response.all_headers()
            body = (await page.content()).encode()
            return BrowserPayload(
                body=body,
                media_type=headers.get("content-type", "text/html").partition(";")[0],
                content_disposition=headers.get("content-disposition", ""),
            )
        finally:
            await page.close()

    async def interact[T](self, operation: Callable[[Page], Awaitable[T]]) -> T:
        """Provide a fresh page without learning authority selectors."""
        page = await self._context.new_page()
        try:
            return await operation(page)
        finally:
            await page.close()

    async def aclose(self) -> None:
        """Close context, browser, and Playwright driver."""
        await self._context.close()
        await self._browser.close()
        await self._playwright.stop()


class PlaywrightPortalSession:
    """Portal session backed by one shared browser worker."""

    def __init__(
        self,
        boundary: BrowserBoundary,
        *,
        worker: BrowserWorker | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Bind a browser lifecycle to the shared worker."""
        self._boundary = boundary
        self._worker = worker or BrowserWorker()
        self._clock = clock
        self._requested_urls: list[str] = []
        self._attachment_body_requests = 0
        self._transferred_bytes = 0
        self._browser_time_ms = 0

    @classmethod
    async def create(
        cls,
        *,
        worker: BrowserWorker | None = None,
    ) -> PlaywrightPortalSession:
        """Create the production Playwright boundary lazily."""
        return cls(await PlaywrightBoundary.create(), worker=worker)

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Render one allowlisted page and retain the resulting HTML."""
        raw_url = str(request.url)
        split = urlsplit(raw_url)
        if _is_attachment_path(split.path):
            self._attachment_body_requests += 1
            raise _attachment_error(split.hostname)
        started = self._clock()
        try:
            payload = await self._worker.run(lambda: self._boundary.open(raw_url))
        finally:
            self._browser_time_ms += max(0, round((self._clock() - started) * 1000))
        if _is_attachment(payload):
            self._attachment_body_requests += 1
            raise _attachment_error(split.hostname)
        self._requested_urls.append(_safe_url(raw_url))
        self._transferred_bytes += len(payload.body)
        return EvidenceCapture(
            url=HttpUrl(_safe_url(raw_url)),
            media_type=payload.media_type,
            body=payload.body,
            digest=EvidenceDigest(sha256(payload.body).hexdigest()),
        )

    async def run_browser[T](self, operation: Callable[[Page], Awaitable[T]]) -> T:
        """Run a semantic authority page object in the shared browser slot."""
        boundary = self._boundary
        if not isinstance(boundary, InteractiveBrowserBoundary):
            raise _source_error(None)
        started = self._clock()
        try:
            return await self._worker.run(lambda: boundary.interact(operation))
        finally:
            self._browser_time_ms += max(0, round((self._clock() - started) * 1000))

    def retain_rendered(
        self,
        url: str,
        body: bytes,
        *,
        media_type: str = "text/html",
    ) -> EvidenceCapture:
        """Account for rendered markup captured by an authority page object."""
        safe_url = _safe_url(url)
        self._requested_urls.append(safe_url)
        self._transferred_bytes += len(body)
        return EvidenceCapture(
            url=HttpUrl(safe_url),
            media_type=media_type,
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Return rendered URLs without query secrets."""
        return tuple(self._requested_urls)

    @property
    def attachment_body_requests(self) -> int:
        """Return blocked attachment and download attempts."""
        return self._attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        """Return rendered HTML bytes retained by adapters."""
        return self._transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        """Return measured time inside the browser worker."""
        return self._browser_time_ms

    @property
    def mode(self) -> TransportMode:
        """Identify the session as live browser transport."""
        return TransportMode.BROWSER

    async def aclose(self) -> None:
        """Close the browser boundary."""
        await self._boundary.aclose()


def _is_attachment(payload: BrowserPayload) -> bool:
    disposition = payload.content_disposition.lower()
    media_type = payload.media_type.lower()
    return (
        "attachment" in disposition
        or "filename=" in disposition
        or media_type.startswith(_ATTACHMENT_MEDIA_PREFIXES)
    )


def _is_attachment_path(path: str) -> bool:
    lowered = path.casefold()
    return PurePosixPath(path).suffix.lower() in _ATTACHMENT_SUFFIXES or any(
        fragment in lowered for fragment in _ATTACHMENT_PATH_FRAGMENTS
    )


def _safe_url(url: str) -> str:
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def _source_error(host: str | None) -> SourceUnavailableError:
    return SourceUnavailableError(
        f"browser source unavailable: {host or 'unknown host'}"
    )


def _attachment_error(host: str | None) -> AttachmentBodyBlockedError:
    return AttachmentBodyBlockedError(
        f"attachment body blocked for {host or 'unknown host'}"
    )
