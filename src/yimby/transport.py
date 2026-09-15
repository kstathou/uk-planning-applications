# Copyright (c) 2026 Kostas Stathoulopoulos

"""Portal transport boundary and deterministic fixture implementation."""

from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest, FrozenModel, TransportMode


class SourceUnavailableError(RuntimeError):
    """A fixture or live source could not return the requested resource."""


class AttachmentBodyBlockedError(RuntimeError):
    """A request attempted to retrieve attachment content."""


class RequestIntent(StrEnum):
    """Permitted reasons for retrieving portal content."""

    SEARCH = "search"
    DETAIL = "detail"
    COMMENTS = "comments"


class PortalRequest(FrozenModel):
    """One allowlisted portal request."""

    url: HttpUrl
    intent: RequestIntent


class FixtureResponse(FrozenModel):
    """Sanitised response used by deterministic tests."""

    body: bytes
    media_type: str = "text/html"


class PortalSession(Protocol):
    """Narrow transport supplied to authority adapters."""

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Retrieve and retain one permitted response."""

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Return URLs whose bodies were retrieved."""

    @property
    def attachment_body_requests(self) -> int:
        """Return the number of blocked attachment-body attempts."""

    @property
    def transferred_bytes(self) -> int:
        """Return response-body bytes transferred through this session."""

    @property
    def browser_time_ms(self) -> int:
        """Return wall time spent inside a browser worker."""

    @property
    def mode(self) -> TransportMode:
        """Identify whether fixture or live transport served the run."""

    async def aclose(self) -> None:
        """Release transport resources."""


class FixtureSession:
    """Replay sanitised source responses without network access."""

    def __init__(self, responses: dict[str, FixtureResponse]) -> None:
        """Index fixture responses by their exact requested URL."""
        self._responses = responses
        self._requested_urls: list[str] = []
        self._attachment_body_requests = 0
        self._transferred_bytes = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Return one fixture response after applying attachment policy."""
        url = str(request.url)
        suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
        if suffix in {".doc", ".docx", ".pdf", ".xls", ".xlsx", ".zip"}:
            self._attachment_body_requests += 1
            raise AttachmentBodyBlockedError(url)
        response = self._responses.get(url)
        if response is None:
            raise SourceUnavailableError(url)
        self._requested_urls.append(url)
        self._transferred_bytes += len(response.body)
        digest = EvidenceDigest(sha256(response.body).hexdigest())
        return EvidenceCapture(
            url=request.url,
            media_type=response.media_type,
            body=response.body,
            digest=digest,
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Return retrieved URLs in request order."""
        return tuple(self._requested_urls)

    @property
    def available_urls(self) -> tuple[str, ...]:
        """Return fixture URLs in declaration order."""
        return tuple(self._responses)

    @property
    def attachment_body_requests(self) -> int:
        """Return blocked attachment-body attempts."""
        return self._attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        """Return fixture bytes replayed to adapters."""
        return self._transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        """Fixture replay does not use a browser."""
        return 0

    @property
    def mode(self) -> TransportMode:
        """Identify this deterministic session as fixture-backed."""
        return TransportMode.FIXTURE

    async def aclose(self) -> None:
        """Fixture sessions hold no external resources."""
