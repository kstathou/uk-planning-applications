# Copyright (c) 2026 Kostas Stathoulopoulos

"""Portal transport boundary and deterministic fixture implementation."""

from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, model_validator

from yimby.domain import EvidenceCapture, EvidenceDigest, FrozenModel, TransportMode

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


class SourceUnavailableError(RuntimeError):
    """A fixture or live source could not return the requested resource."""


class AttachmentBodyBlockedError(RuntimeError):
    """A request attempted to retrieve attachment content."""


class RedirectBoundaryError(RuntimeError):
    """A redirect target fell outside the request's explicit live boundary."""


class RequestIntent(StrEnum):
    """Permitted reasons for retrieving portal content."""

    SEARCH = "search"
    DETAIL = "detail"
    COMMENTS = "comments"


class RequestMethod(StrEnum):
    """HTTP methods permitted by the portal boundary."""

    GET = "GET"
    POST = "POST"


class FormField(FrozenModel):
    """One typed form field without headers or logging behaviour."""

    name: str
    value: str


class RequestHeader(FrozenModel):
    """One public portal-routing header permitted at the transport boundary."""

    name: Literal["x-client", "x-product", "x-service"]
    value: str = Field(min_length=1)


class RedirectBoundary(FrozenModel):
    """Origin and paths a live request may follow through redirects."""

    origin: HttpUrl
    exact_paths: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    query_paths: tuple[str, ...] | None = None

    def allows(self, url: str) -> bool:
        """Return whether a destination remains inside the declared boundary."""
        expected = urlsplit(str(self.origin))
        candidate = urlsplit(url)
        same_origin = (
            candidate.scheme,
            candidate.hostname,
            candidate.port,
            candidate.username,
            candidate.password,
        ) == (
            expected.scheme,
            expected.hostname,
            expected.port,
            None,
            None,
        )
        allowed_path = candidate.path in self.exact_paths or any(
            candidate.path.startswith(prefix) for prefix in self.path_prefixes
        )
        query_allowed = (
            not candidate.query
            or self.query_paths is None
            or candidate.path in self.query_paths
        )
        return same_origin and not candidate.fragment and allowed_path and query_allowed


class PortalRequest(FrozenModel):
    """One allowlisted portal request."""

    url: HttpUrl
    intent: RequestIntent
    method: RequestMethod = RequestMethod.GET
    form: tuple[FormField, ...] = ()
    headers: tuple[RequestHeader, ...] = ()
    redirect_boundary: RedirectBoundary | None = None

    @model_validator(mode="after")
    def form_requires_post(self) -> Self:
        """Reject ambiguous GET requests carrying a form body."""
        if self.method == RequestMethod.GET and self.form:
            message = "GET portal requests cannot carry form fields"
            raise ValueError(message)
        names = tuple(header.name for header in self.headers)
        if len(names) != len(set(names)):
            message = "duplicate request header"
            raise ValueError(message)
        return self


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
        if request.headers:
            message = "fixture transport does not support request headers"
            raise ValueError(message)
        url = str(request.url)
        path = urlsplit(url).path
        lowered = path.casefold()
        if PurePosixPath(path).suffix.lower() in _ATTACHMENT_SUFFIXES or any(
            fragment in lowered for fragment in _ATTACHMENT_PATH_FRAGMENTS
        ):
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
