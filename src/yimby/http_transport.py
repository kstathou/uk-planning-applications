# Copyright (c) 2026 Kostas Stathoulopoulos

"""Rate-limited HTTP transport for explicitly live-ready authorities."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest, TransportMode
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalRequest,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

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
    "/api/application/document/opdc/",
)
_ATTACHMENT_MEDIA_TYPES = {
    "application/msword",
    "application/octet-stream",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip",
}
_ATTACHMENT_MEDIA_PREFIXES = ("audio/", "image/", "video/")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SUCCESS_MIN = 200
_SUCCESS_MAX = 300
_MAX_ATTEMPTS = 5
_MAX_RETRY_DELAY = 60.0
_DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "user-agent": "yimby/0.1 (+local planning research; contact via source repository)",
}


class HostRateLimiter:
    """Serialize each host and enforce a minimum post-request gap."""

    def __init__(
        self,
        minimum_gap: float = 2.0,
        *,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Configure a limiter with injectable deterministic time."""
        self._minimum_gap = minimum_gap
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_finished: dict[str, float] = {}

    @asynccontextmanager
    async def turn(self, host: str) -> AsyncIterator[None]:
        """Wait for and hold the exclusive request slot for a host."""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last_finished = self._last_finished.get(host)
            if last_finished is not None:
                delay = self._minimum_gap - (self._clock() - last_finished)
                if delay > 0:
                    await self._sleep(delay)
            try:
                yield
            finally:
                self._last_finished[host] = self._clock()


class HttpxPortalSession:
    """Persistent-cookie live HTTP session with bounded retries."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: HostRateLimiter | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Configure one persistent client and a bounded retry policy."""
        if not 1 <= max_attempts <= _MAX_ATTEMPTS:
            msg = "max_attempts must be between one and five"
            raise ValueError(msg)
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
            timeout=httpx.Timeout(30.0),
        )
        self._limiter = limiter or HostRateLimiter()
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._now = now
        self._requested_urls: list[str] = []
        self._attachment_body_requests = 0
        self._transferred_bytes = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Fetch one HTML or JSON response without reading attachments."""
        raw_url = str(request.url)
        split = urlsplit(raw_url)
        if _is_attachment_path(split.path):
            self._attachment_body_requests += 1
            raise _attachment_error(split.hostname)
        body, media_type = await self._read_with_retries(
            request,
            raw_url,
            split.hostname or "",
        )
        self._requested_urls.append(_safe_url(raw_url))
        self._transferred_bytes += len(body)
        return EvidenceCapture(
            url=HttpUrl(_safe_url(raw_url)),
            media_type=media_type,
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

    async def _read_with_retries(
        self,
        portal_request: PortalRequest,
        url: str,
        host: str,
    ) -> tuple[bytes, str]:
        """Hold the shared host slot through body reads and retry cooldowns."""
        last_status: int | None = None
        safe_url = _safe_url(url)
        form = [(field.name, field.value) for field in portal_request.form]
        encoded_form = urlencode(form).encode() if form else None
        headers: dict[str, str] = {
            header.name: header.value for header in portal_request.headers
        }
        if form:
            headers["content-type"] = "application/x-www-form-urlencoded"
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._limiter.turn(host):
                    response: httpx.Response | None = None
                    try:
                        response = await self._client.send(
                            self._client.build_request(
                                portal_request.method,
                                url,
                                content=encoded_form,
                                headers=headers,
                            ),
                            stream=True,
                        )
                        last_status = response.status_code
                        if (
                            response.status_code in _RETRYABLE_STATUS
                            and attempt < self._max_attempts
                        ):
                            retry_after = _retry_after_seconds(
                                response.headers.get("retry-after"), self._now()
                            )
                            await response.aclose()
                            response = None
                            await self._sleep(
                                retry_after
                                if retry_after is not None
                                else _backoff(attempt)
                            )
                            continue
                        if not _SUCCESS_MIN <= response.status_code < _SUCCESS_MAX:
                            raise _status_error(safe_url, response.status_code)
                        final_url = urlsplit(str(response.url))
                        if _is_attachment_path(
                            final_url.path
                        ) or _is_attachment_response(response):
                            self._attachment_body_requests += 1
                            raise _attachment_error(final_url.hostname or host)
                        body = await response.aread()
                        media_type = response.headers.get(
                            "content-type", "application/octet-stream"
                        )
                        return body, media_type.partition(";")[0].strip().lower()
                    finally:
                        if response is not None:
                            await response.aclose()
            except httpx.TransportError as error:
                if attempt == self._max_attempts:
                    raise _attempts_error(safe_url, attempt) from error
                await self._sleep(_backoff(attempt))
                continue
        raise _status_error(safe_url, last_status)  # pragma: no cover

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Return successfully retrieved URLs without query secrets."""
        return tuple(self._requested_urls)

    @property
    def attachment_body_requests(self) -> int:
        """Return attachment responses blocked before body reads."""
        return self._attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        """Return bytes whose bodies were deliberately read."""
        return self._transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        """HTTP requests consume no browser worker time."""
        return 0

    @property
    def mode(self) -> TransportMode:
        """Identify the session as live HTTP."""
        return TransportMode.LIVE

    async def aclose(self) -> None:
        """Close the persistent-cookie client."""
        await self._client.aclose()


def _is_attachment_response(response: httpx.Response) -> bool:
    disposition = response.headers.get("content-disposition", "").lower()
    media_type = response.headers.get("content-type", "").partition(";")[0].lower()
    return (
        "attachment" in disposition
        or "filename=" in disposition
        or media_type in _ATTACHMENT_MEDIA_TYPES
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


def _backoff(attempt: int) -> float:
    return min(5.0 * float(2 ** (attempt - 1)), 30.0)


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        return min(_MAX_RETRY_DELAY, max(0.0, float(value)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return min(
            _MAX_RETRY_DELAY,
            max(0.0, (parsed - now).total_seconds()),
        )


def _attachment_error(host: str | None) -> AttachmentBodyBlockedError:
    return AttachmentBodyBlockedError(
        f"attachment body blocked for {host or 'unknown host'}"
    )


def _attempts_error(safe_url: str, attempts: int) -> SourceUnavailableError:
    return SourceUnavailableError(
        f"source unavailable: {safe_url} after {attempts} attempts"
    )


def _status_error(safe_url: str, status: int | None) -> SourceUnavailableError:
    return SourceUnavailableError(
        f"source unavailable: {safe_url} HTTP {status or 'unknown'}"
    )
