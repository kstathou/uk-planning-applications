# Copyright (c) 2026 Kostas Stathoulopoulos

"""Portal transport boundary and deterministic fixture implementation."""

from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest, FrozenModel


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


class FixtureSession:
    """Replay sanitised source responses without network access."""

    def __init__(self, responses: dict[str, FixtureResponse]) -> None:
        """Index fixture responses by their exact requested URL."""
        self._responses = responses
        self._requested_urls: list[str] = []
        self._attachment_body_requests = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Return one fixture response after applying attachment policy."""
        url = str(request.url)
        suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
        if suffix in {".pdf", ".doc", ".docx"}:
            self._attachment_body_requests += 1
            raise AttachmentBodyBlockedError(url)
        response = self._responses.get(url)
        if response is None:
            raise SourceUnavailableError(url)
        self._requested_urls.append(url)
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
    def attachment_body_requests(self) -> int:
        """Return blocked attachment-body attempts."""
        return self._attachment_body_requests
