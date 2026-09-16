# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ARG001, ARG002, EM101, PLR0915, PLR2004, RUF043, SIM117, SLF001, TC003, TRY003

"""Live-boundary, orchestration, and Streamlit presentation behaviour."""

from __future__ import annotations

import asyncio
import fcntl
import os
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import HttpUrl

from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.barnet.fixtures import fixture_session
from yimby.browser_transport import (
    BrowserPayload,
    BrowserWorker,
    PlaywrightBoundary,
    PlaywrightPortalSession,
)
from yimby.cli import main as cli_main
from yimby.dashboard import dashboard_snapshot
from yimby.dashboard_app import launch_dashboard, main, render_dashboard, run_dashboard
from yimby.domain import (
    ApplicationId,
    ApplicationLocation,
    ApplicationSearchHit,
    AuthorityCollectionStatus,
    AuthorityId,
    DashboardSnapshot,
    DiscoveryWindow,
    EvidenceCapture,
    LiveReadiness,
    LiveStatus,
    LiveTransportKind,
    TransportMode,
    Wgs84Coordinate,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import (
    HostRateLimiter,
    HttpxPortalSession,
    _backoff,
    _retry_after_seconds,
)
from yimby.orchestration import (
    CollectionAlreadyRunningError,
    CollectionOrchestrator,
    LiveSessionFactory,
    ProcessLock,
)
from yimby.pilot_fixtures import FIXTURE_BUILDERS
from yimby.portal_time import england_calendar_date
from yimby.registry import AuthorityRegistry, pilot_registry
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    FixtureResponse,
    FixtureSession,
    PortalRequest,
    RequestHeader,
    RequestIntent,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from playwright.async_api import Browser, BrowserContext, Playwright

    from yimby.dashboard_app import DashboardSurface
    from yimby.transport import PortalSession

WINDOW = DiscoveryWindow(start=date(2026, 8, 16), end=date(2026, 9, 15))


def _request(url: str) -> PortalRequest:
    return PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)


def _store(root: Path) -> SqliteStore:
    return SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))


def test_english_portal_calendar_uses_london_civil_time() -> None:
    """BST midnight boundaries follow the authority's civil calendar."""
    assert england_calendar_date(datetime(2026, 6, 1, 23, 30, tzinfo=UTC)) == date(
        2026, 6, 2
    )
    assert england_calendar_date(datetime(2026, 1, 1, 0, 30, tzinfo=UTC)) == date(
        2026, 1, 1
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        england_calendar_date(
            datetime(2026, 6, 1, 23, 30, tzinfo=UTC).replace(tzinfo=None)
        )


def _registry(status: LiveStatus) -> AuthorityRegistry:
    return AuthorityRegistry(
        (BARNET_PACKAGE,),
        {AuthorityId("barnet"): status},
    )


def _live_status(
    transport: LiveTransportKind | None = LiveTransportKind.HTTP,
) -> LiveStatus:
    return LiveStatus(
        readiness=LiveReadiness.LIVE_READY,
        reason="deterministic test contract",
        evidence=("unit test",),
        transport=transport,
    )


def test_pilot_live_readiness_is_truthful_and_persisted(tmp_path: Path) -> None:
    """Only receipt-qualified authorities are live-ready; other gaps stay explicit."""
    registry = pilot_registry()
    readiness_by_authority = {
        manifest.id: manifest.live_status.readiness for manifest in registry.manifests()
    }
    assert readiness_by_authority == {
        AuthorityId("barnet"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("camden"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("haringey"): LiveReadiness.BROWSER_ONLY,
        AuthorityId("devon"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("peak-district"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("arun"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("opdc"): LiveReadiness.LIVE_READY,
        AuthorityId("dorset"): LiveReadiness.BROWSER_ONLY,
        AuthorityId("cheshire-east"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("blackburn-with-darwen"): LiveReadiness.BLOCKED,
        AuthorityId("birmingham"): LiveReadiness.BLOCKED,
        AuthorityId("leeds"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("cornwall"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("durham"): LiveReadiness.DISCOVERY_ONLY,
        AuthorityId("west-suffolk"): LiveReadiness.LIVE_READY,
    }
    assert registry.manifest(AuthorityId("west-suffolk")).live_status == LiveStatus(
        readiness=LiveReadiness.LIVE_READY,
        reason="live bootstrap and zero-request rerun passed on 16 September 2026",
        evidence=("docs/evidence/west-suffolk-qualification-2026-09-16.json",),
        transport=LiveTransportKind.HTTP,
    )
    assert registry.manifest(AuthorityId("opdc")).live_status == LiveStatus(
        readiness=LiveReadiness.LIVE_READY,
        reason="official Agile API bootstrap and immediate idempotent rerun qualified",
        evidence=(
            (
                "docs/evidence/opdc-qualification-2026-09-16.json records "
                "55 complete applications"
            ),
        ),
        transport=LiveTransportKind.HTTP,
    )
    store = _store(tmp_path)
    store.register_authorities(registry.manifests())
    snapshot = dashboard_snapshot(store, registry)
    assert snapshot.coverage_implemented == 15
    assert snapshot.live_ready == 2
    assert snapshot.live_readiness_denominator == 15
    assert snapshot.browser_time_ms == 0
    assert all(row.live_reason and row.live_evidence for row in snapshot.authorities)
    store.close()


def test_host_limiter_enforces_gap_and_one_in_flight() -> None:
    """A host cannot overlap requests and receives a post-request gap."""
    current = [10.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        current[0] += delay

    limiter = HostRateLimiter(2.0, clock=lambda: current[0], sleep=sleep)

    async def exercise_gap() -> None:
        async with limiter.turn("example.test"):
            current[0] += 0.5
        async with limiter.turn("example.test"):
            current[0] += 0.25
        current[0] += 3.0
        async with limiter.turn("example.test"):
            pass

    asyncio.run(exercise_gap())
    assert sleeps == [2.0]

    active = 0
    maximum = 0
    gate = asyncio.Event()
    serialized = HostRateLimiter(0)

    async def operation() -> None:
        nonlocal active, maximum
        async with serialized.turn("same.test"):
            active += 1
            maximum = max(maximum, active)
            gate.set()
            await asyncio.sleep(0)
            active -= 1

    async def exercise_overlap() -> None:
        first = asyncio.create_task(operation())
        await gate.wait()
        second = asyncio.create_task(operation())
        await asyncio.gather(first, second)

    asyncio.run(exercise_overlap())
    assert maximum == 1


def test_http_session_cookies_accounting_and_secret_redaction() -> None:
    """The HTTP boundary keeps cookies but never exposes query credentials."""
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        headers = (
            {"set-cookie": "portal=ready; Path=/", "content-type": "text/html; utf-8"}
            if len(cookies) == 1
            else {"content-type": "text/html"}
        )
        return httpx.Response(200, headers=headers, content=b"<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = HttpxPortalSession(
        client=client,
        limiter=HostRateLimiter(0),
    )

    async def exercise() -> None:
        first = await session.fetch(
            _request("https://example.test/first?token=top-secret")
        )
        second = await session.fetch(_request("https://example.test/second"))
        assert first.media_type == "text/html"
        assert "top-secret" not in str(first.url)
        assert second.body == b"<html>ok</html>"
        await session.aclose()

    asyncio.run(exercise())
    assert cookies == [None, "portal=ready"]
    assert session.requested_urls == (
        "https://example.test/first",
        "https://example.test/second",
    )
    assert "top-secret" not in repr(session.requested_urls)
    assert session.transferred_bytes == 30
    assert session.attachment_body_requests == 0
    assert session.browser_time_ms == 0
    assert session.mode == TransportMode.LIVE


def test_http_session_sends_only_typed_public_routing_headers() -> None:
    """Authority routing values reach HTTP without widening to credentials."""
    received: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(dict(request.headers))
        return httpx.Response(200, content=b"{}")

    session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=HostRateLimiter(0),
    )
    request = PortalRequest(
        url=HttpUrl("https://planningapi.agileapplications.co.uk/api/application/1"),
        intent=RequestIntent.DETAIL,
        headers=(
            RequestHeader(name="x-client", value="OPDC"),
            RequestHeader(name="x-product", value="CITIZENPORTAL"),
            RequestHeader(name="x-service", value="PA"),
        ),
    )

    async def exercise() -> None:
        await session.fetch(request)
        await session.aclose()

    asyncio.run(exercise())
    assert {
        name: received[0][name] for name in ("x-client", "x-product", "x-service")
    } == {
        "x-client": "OPDC",
        "x-product": "CITIZENPORTAL",
        "x-service": "PA",
    }
    with pytest.raises(ValueError, match="duplicate request header"):
        PortalRequest(
            url=HttpUrl("https://example.test"),
            intent=RequestIntent.SEARCH,
            headers=(
                RequestHeader(name="x-client", value="OPDC"),
                RequestHeader(name="x-client", value="OTHER"),
            ),
        )


def test_shared_host_limiter_covers_stream_consumption() -> None:
    """A second session cannot enter a host while the first body is streaming."""

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        requests = 0

        class GatedStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                started.set()
                try:
                    await release.wait()
                    yield b"complete"
                finally:
                    active -= 1

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, stream=GatedStream())

        limiter = HostRateLimiter(0)
        transport = httpx.MockTransport(handler)
        first = HttpxPortalSession(
            client=httpx.AsyncClient(transport=transport), limiter=limiter
        )
        second = HttpxPortalSession(
            client=httpx.AsyncClient(transport=transport), limiter=limiter
        )
        first_task = asyncio.create_task(first.fetch(_request("https://same.test/a")))
        await started.wait()
        second_task = asyncio.create_task(second.fetch(_request("https://same.test/b")))
        await asyncio.sleep(0)
        assert requests == 1
        assert maximum == 1
        release.set()
        results = await asyncio.gather(first_task, second_task)
        assert [result.body for result in results] == [b"complete", b"complete"]
        assert requests == 2
        assert maximum == 1
        await first.aclose()
        await second.aclose()

    asyncio.run(exercise())


def test_retry_after_cooldown_is_shared_by_host() -> None:
    """A Retry-After delay blocks peer sessions using the same host limiter."""

    async def exercise() -> None:
        cooldown_started = asyncio.Event()
        release_cooldown = asyncio.Event()
        paths: list[str] = []
        limited_attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal limited_attempts
            paths.append(request.url.path)
            if request.url.path == "/limited":
                limited_attempts += 1
                if limited_attempts == 1:
                    return httpx.Response(429, headers={"retry-after": "3"})
            return httpx.Response(200, content=b"ok")

        async def cooldown(delay: float) -> None:
            assert delay == 3
            cooldown_started.set()
            await release_cooldown.wait()

        limiter = HostRateLimiter(0)
        transport = httpx.MockTransport(handler)
        limited = HttpxPortalSession(
            client=httpx.AsyncClient(transport=transport),
            limiter=limiter,
            sleep=cooldown,
        )
        peer = HttpxPortalSession(
            client=httpx.AsyncClient(transport=transport), limiter=limiter
        )
        limited_task = asyncio.create_task(
            limited.fetch(_request("https://same.test/limited"))
        )
        await cooldown_started.wait()
        peer_task = asyncio.create_task(peer.fetch(_request("https://same.test/peer")))
        await asyncio.sleep(0)
        assert paths == ["/limited"]
        release_cooldown.set()
        await asyncio.gather(limited_task, peer_task)
        assert paths[0] == "/limited"
        assert set(paths[1:]) == {"/limited", "/peer"}
        await limited.aclose()
        await peer.aclose()

    asyncio.run(exercise())


def test_http_session_default_client_identifies_the_collector() -> None:
    """Live requests use an explicit, honest identity accepted by public portals."""
    session = HttpxPortalSession()
    assert session._client.headers["user-agent"].startswith("yimby/0.1 ")
    assert "text/html" in session._client.headers["accept"]
    asyncio.run(session.aclose())


@pytest.mark.parametrize(
    ("url", "headers", "network_calls"),
    [
        ("https://example.test/file.pdf", {}, 0),
        ("https://example.test/file.png", {}, 0),
        ("https://example.test/Document/Download?id=1", {}, 0),
        ("https://example.test/api/application/document/OPDC/123", {}, 0),
        ("https://example.test/view", {"content-disposition": "attachment"}, 1),
        ("https://example.test/view", {"content-disposition": "filename=x.txt"}, 1),
        ("https://example.test/view", {"content-type": "application/pdf"}, 1),
        ("https://example.test/view", {"content-type": "image/jpeg"}, 1),
    ],
)
def test_http_session_blocks_attachment_bodies(
    url: str,
    headers: dict[str, str],
    network_calls: int,
) -> None:
    """Path and response metadata stop attachment content before retention."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers=headers, content=b"secret body")

    session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=HostRateLimiter(0),
    )

    async def exercise() -> None:
        with pytest.raises(AttachmentBodyBlockedError, match="example.test"):
            await session.fetch(_request(url))
        await session.aclose()

    asyncio.run(exercise())
    assert calls == network_calls
    assert session.transferred_bytes == 0
    assert session.attachment_body_requests == 1


def test_browser_session_rejects_request_headers_it_cannot_apply() -> None:
    """A browser session cannot silently discard authority routing headers."""
    session = PlaywrightPortalSession(_FakeBoundary())

    async def exercise() -> None:
        with pytest.raises(ValueError, match="request headers"):
            await session.fetch(
                PortalRequest(
                    url=HttpUrl("https://browser.test/page"),
                    intent=RequestIntent.DETAIL,
                    headers=(RequestHeader(name="x-client", value="OPDC"),),
                )
            )

    asyncio.run(exercise())


def test_fixture_session_rejects_request_headers_it_cannot_apply() -> None:
    """Fixture replay cannot silently discard authority routing headers."""
    url = "https://fixture.test/page"
    session = FixtureSession({url: FixtureResponse(body=b"fixture")})

    async def exercise() -> None:
        with pytest.raises(ValueError, match="request headers"):
            await session.fetch(
                PortalRequest(
                    url=HttpUrl(url),
                    intent=RequestIntent.DETAIL,
                    headers=(RequestHeader(name="x-client", value="OPDC"),),
                )
            )

    asyncio.run(exercise())


def test_http_session_blocks_redirect_to_attachment_path() -> None:
    """A safe-looking route cannot redirect into an unlabelled file body."""
    body_reads = 0

    class ForbiddenStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal body_reads
            body_reads += 1
            yield b"must not be read"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/hidden/file.pdf"})
        return httpx.Response(200, stream=ForbiddenStream())

    session = HttpxPortalSession(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
        limiter=HostRateLimiter(0),
    )

    async def exercise() -> None:
        with pytest.raises(AttachmentBodyBlockedError, match="example.test"):
            await session.fetch(_request("https://example.test/start"))
        await session.aclose()

    asyncio.run(exercise())
    assert body_reads == 0
    assert session.transferred_bytes == 0
    assert session.attachment_body_requests == 1


def test_http_session_retry_after_and_transport_failures() -> None:
    """Retries are bounded and honor numeric and dated Retry-After values."""
    attempts = 0
    sleeps: list[float] = []
    now = datetime(2026, 9, 16, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        if attempts == 2:
            return httpx.Response(
                503,
                headers={"retry-after": format_datetime(now + timedelta(seconds=4))},
            )
        return httpx.Response(200, content=b"done")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=HostRateLimiter(0),
        sleep=sleep,
        now=lambda: now,
    )

    async def exercise_retry_after() -> None:
        assert (
            await session.fetch(_request("https://retry.test/page"))
        ).body == b"done"
        await session.aclose()

    asyncio.run(exercise_retry_after())
    assert attempts == 3
    assert sleeps == [3.0, 4.0]

    transport_attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal transport_attempts
        transport_attempts += 1
        if transport_attempts == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(200, content=b"recovered")

    recovered = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(flaky)),
        limiter=HostRateLimiter(0),
        max_attempts=2,
        sleep=sleep,
    )

    async def exercise_transport_retry() -> None:
        assert (
            await recovered.fetch(_request("https://retry.test/recovered"))
        ).body == b"recovered"
        await recovered.aclose()

    asyncio.run(exercise_transport_retry())
    assert transport_attempts == 2


def test_http_session_errors_are_bounded_and_sanitised() -> None:
    """Final status and transport failures disclose no URL secrets."""
    with pytest.raises(ValueError, match="between one and five"):
        HttpxPortalSession(max_attempts=0)
    with pytest.raises(ValueError, match="between one and five"):
        HttpxPortalSession(max_attempts=6)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    status_session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(unavailable)),
        limiter=HostRateLimiter(0),
    )

    async def status_error() -> None:
        with pytest.raises(SourceUnavailableError) as captured:
            await status_session.fetch(
                _request("https://failed.test/page?token=do-not-log")
            )
        assert "do-not-log" not in str(captured.value)
        assert "HTTP 404" in str(captured.value)
        await status_session.aclose()

    asyncio.run(status_error())

    def retryable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"retry-after": "invalid"})

    retry_session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(retryable)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )

    async def retry_error() -> None:
        with pytest.raises(SourceUnavailableError, match="HTTP 503"):
            await retry_session.fetch(_request("https://failed.test/retry"))
        await retry_session.aclose()

    asyncio.run(retry_error())

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret transport detail", request=request)

    broken_session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )

    async def transport_error() -> None:
        with pytest.raises(
            SourceUnavailableError, match="after 1 attempts"
        ) as captured:
            await broken_session.fetch(_request("https://failed.test/transport"))
        assert "secret transport detail" not in str(captured.value)
        await broken_session.aclose()

    asyncio.run(transport_error())


def test_retry_after_parsing_covers_invalid_naive_and_past_dates() -> None:
    """Retry-After parsing is bounded at zero for every accepted form."""
    now = datetime(2026, 9, 16, tzinfo=UTC)
    assert _retry_after_seconds(None, now) is None
    assert _retry_after_seconds("-2", now) == 0
    assert _retry_after_seconds("999", now) == 60
    assert _retry_after_seconds("nonsense", now) is None
    assert _retry_after_seconds("Tue, 15 Sep 2026 00:00:00 GMT", now) == 0
    assert _retry_after_seconds("15 Sep 2026 00:00:02", now) == 0
    assert _backoff(1) == 5.0
    assert _backoff(4) == 30.0


class _FakeBoundary:
    def __init__(
        self,
        payload: BrowserPayload | None = None,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload or BrowserPayload(
            body=b"<html>rendered</html>", media_type="text/html"
        )
        self.error = error
        self.closed = False

    async def open(self, url: str) -> BrowserPayload:
        if self.error is not None:
            raise self.error
        return self.payload

    async def aclose(self) -> None:
        self.closed = True


def test_browser_session_serialises_accounts_and_blocks_attachments() -> None:
    """Browser rendering is single-worker, measured, and attachment safe."""
    clock_values = iter((1.0, 1.25))
    boundary = _FakeBoundary()
    session = PlaywrightPortalSession(boundary, clock=lambda: next(clock_values))

    async def success() -> None:
        capture = await session.fetch(
            _request("https://browser.test/page?session=secret")
        )
        assert capture.body == b"<html>rendered</html>"
        assert "secret" not in str(capture.url)
        await session.aclose()

    asyncio.run(success())
    assert boundary.closed
    assert session.requested_urls == ("https://browser.test/page",)
    assert session.transferred_bytes == len(b"<html>rendered</html>")
    assert session.browser_time_ms == 250
    assert session.mode == TransportMode.BROWSER

    path_session = PlaywrightPortalSession(_FakeBoundary())
    disposition_session = PlaywrightPortalSession(
        _FakeBoundary(
            BrowserPayload(
                body=b"not retained",
                media_type="text/plain",
                content_disposition="filename=document.txt",
            )
        )
    )
    media_session = PlaywrightPortalSession(
        _FakeBoundary(
            BrowserPayload(body=b"not retained", media_type="application/pdf")
        )
    )
    image_media_session = PlaywrightPortalSession(
        _FakeBoundary(BrowserPayload(body=b"not retained", media_type="image/jpeg"))
    )

    async def blocked() -> None:
        for current, url in (
            (path_session, "https://browser.test/file.docx"),
            (path_session, "https://browser.test/file.jpeg"),
            (path_session, "https://browser.test/Document/Download?id=1"),
            (disposition_session, "https://browser.test/view"),
            (media_session, "https://browser.test/view"),
            (image_media_session, "https://browser.test/view"),
        ):
            with pytest.raises(AttachmentBodyBlockedError):
                await current.fetch(_request(url))

    asyncio.run(blocked())
    assert path_session.attachment_body_requests == 3
    assert disposition_session.attachment_body_requests == 1
    assert media_session.attachment_body_requests == 1
    assert image_media_session.attachment_body_requests == 1


def test_browser_worker_serialises_and_failed_time_is_measured() -> None:
    """A shared worker permits one operation and metrics survive failures."""
    active = 0
    maximum = 0
    worker = BrowserWorker()

    async def operation() -> int:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return 1

    async def exercise_worker() -> None:
        assert tuple(
            await asyncio.gather(
                worker.run(operation),
                worker.run(operation),
            )
        ) == (1, 1)

    asyncio.run(exercise_worker())
    assert maximum == 1

    clock_values = iter((5.0, 5.1))
    failed = PlaywrightPortalSession(
        _FakeBoundary(error=SourceUnavailableError("failed")),
        clock=lambda: next(clock_values),
    )

    async def exercise_failure() -> None:
        with pytest.raises(SourceUnavailableError):
            await failed.fetch(_request("https://browser.test/fail"))

    asyncio.run(exercise_failure())
    assert failed.browser_time_ms == 100


def test_playwright_production_boundary_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow production boundary launches, routes, renders, and closes."""
    response = MagicMock()
    response.all_headers = AsyncMock(return_value={"content-type": "text/html"})
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.content = AsyncMock(return_value="<html>live</html>")
    page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
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
    monkeypatch.setattr(
        "yimby.browser_transport.async_playwright",
        lambda: starter,
    )

    async def exercise() -> None:
        boundary = await PlaywrightBoundary.create()
        route_handler = context.route.await_args.args[1]
        blocked_route = MagicMock()
        blocked_route.request.url = "https://browser.test/file.pdf"
        blocked_route.request.resource_type = "document"
        blocked_route.abort = AsyncMock()
        blocked_route.continue_ = AsyncMock()
        await route_handler(blocked_route)
        blocked_route.abort.assert_awaited_once()

        image_route = MagicMock()
        image_route.request.url = "https://browser.test/page-image"
        image_route.request.resource_type = "image"
        image_route.abort = AsyncMock()
        image_route.continue_ = AsyncMock()
        await route_handler(image_route)
        image_route.abort.assert_awaited_once()

        html_route = MagicMock()
        html_route.request.url = "https://browser.test/page"
        html_route.request.resource_type = "document"
        html_route.abort = AsyncMock()
        html_route.continue_ = AsyncMock()
        await route_handler(html_route)
        html_route.continue_.assert_awaited_once()

        payload = await boundary.open("https://browser.test/page")
        assert payload.body == b"<html>live</html>"
        await boundary.aclose()

    asyncio.run(exercise())
    context.route.assert_awaited_once()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()

    missing_page = MagicMock()
    missing_page.goto = AsyncMock(return_value=None)
    missing_page.close = AsyncMock()
    missing_context = MagicMock()
    missing_context.new_page = AsyncMock(return_value=missing_page)
    missing_boundary = PlaywrightBoundary(
        cast("Playwright", playwright),
        cast("Browser", browser),
        cast("BrowserContext", missing_context),
    )

    async def missing_response() -> None:
        with pytest.raises(SourceUnavailableError, match="browser.test"):
            await missing_boundary.open("https://browser.test/missing")

    asyncio.run(missing_response())
    missing_page.close.assert_awaited_once()

    created_boundary = _FakeBoundary()
    monkeypatch.setattr(
        PlaywrightBoundary,
        "create",
        AsyncMock(return_value=created_boundary),
    )

    async def create_session() -> None:
        created_session = await PlaywrightPortalSession.create()
        await created_session.aclose()

    asyncio.run(create_session())
    assert created_boundary.closed


def test_process_lock_uses_kernel_ownership_and_fails_closed(tmp_path: Path) -> None:
    """Kernel ownership serialises callers even during malformed PID writes."""
    lock_path = tmp_path / "collection.lock"
    with ProcessLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())
        with pytest.raises(CollectionAlreadyRunningError, match="pid"):
            with ProcessLock(lock_path):
                pass
    assert lock_path.read_text() == ""

    lock_path.write_text("invalid")
    with ProcessLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())

    unheld = ProcessLock(lock_path)
    unheld.__exit__(None, None, None)

    descriptor = os.open(lock_path, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(descriptor, 0)
    os.write(descriptor, b"invalid")
    try:
        with pytest.raises(CollectionAlreadyRunningError, match="concurrently"):
            with ProcessLock(lock_path):
                pass
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    target = tmp_path / "must-remain.txt"
    target.write_text("preserve me")
    symlink = tmp_path / "symlink.lock"
    symlink.symlink_to(target)
    with pytest.raises(CollectionAlreadyRunningError, match="regular file"):
        with ProcessLock(symlink):
            pass
    assert target.read_text() == "preserve me"

    fifo = tmp_path / "fifo.lock"
    os.mkfifo(fifo)
    with pytest.raises(CollectionAlreadyRunningError, match="regular file"):
        with ProcessLock(fifo):
            pass


def test_process_lock_releases_after_pid_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed diagnostic write cannot strand the advisory lock."""
    lock_path = tmp_path / "collection.lock"
    original_fstat = os.fstat
    original_flock = fcntl.flock
    original_write = os.write

    monkeypatch.setattr("os.fstat", MagicMock(side_effect=OSError("fstat failed")))
    with pytest.raises(OSError, match="fstat failed"):
        with ProcessLock(lock_path):
            pass
    monkeypatch.setattr("os.fstat", original_fstat)

    monkeypatch.setattr(fcntl, "flock", MagicMock(side_effect=OSError("flock failed")))
    with pytest.raises(OSError, match="flock failed"):
        with ProcessLock(lock_path):
            pass
    monkeypatch.setattr(fcntl, "flock", original_flock)

    monkeypatch.setattr("os.write", MagicMock(side_effect=OSError("write failed")))
    with pytest.raises(OSError, match="write failed"):
        with ProcessLock(lock_path):
            pass
    monkeypatch.setattr("os.write", original_write)
    with ProcessLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())


def test_process_lock_closes_descriptor_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlock errors cannot leak a descriptor or strand kernel ownership."""
    lock_path = tmp_path / "collection.lock"
    original_flock = fcntl.flock
    original_write = os.write

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("unlock failed")
        original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", fail_unlock)
    monkeypatch.setattr("os.write", MagicMock(side_effect=OSError("write failed")))
    with pytest.raises(OSError, match="unlock failed"):
        with ProcessLock(lock_path):
            pass

    monkeypatch.setattr("os.write", original_write)
    held = ProcessLock(lock_path)
    held.__enter__()
    with pytest.raises(OSError, match="unlock failed"):
        held.__exit__(None, None, None)
    assert held._descriptor is None

    monkeypatch.setattr(fcntl, "flock", original_flock)
    with ProcessLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())


class _CloseFailureSession:
    def __init__(self, inner: FixtureSession) -> None:
        self._inner = inner

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        return await self._inner.fetch(request)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return self._inner.requested_urls

    @property
    def attachment_body_requests(self) -> int:
        return self._inner.attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        return self._inner.transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.FIXTURE

    async def aclose(self) -> None:
        raise RuntimeError("close failed")


class _CancelledSession(_CloseFailureSession):
    def __init__(self) -> None:
        super().__init__(FixtureSession({}))
        self.closed = False

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        self.closed = True


class _TrackingSession:
    def __init__(self, inner: PortalSession, activity: list[int]) -> None:
        self._inner = inner
        self._activity = activity

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self._activity[0] += 1
        self._activity[1] = max(self._activity[1], self._activity[0])
        try:
            await asyncio.sleep(0)
            return await self._inner.fetch(request)
        finally:
            self._activity[0] -= 1

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return self._inner.requested_urls

    @property
    def attachment_body_requests(self) -> int:
        return self._inner.attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        return self._inner.transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        return self._inner.browser_time_ms

    @property
    def mode(self) -> TransportMode:
        return self._inner.mode

    async def aclose(self) -> None:
        await self._inner.aclose()


def test_orchestration_caps_four_and_isolates_one_authority_failure(
    tmp_path: Path,
) -> None:
    """One failed authority does not cancel peers and no fifth runs at once."""
    registry = pilot_registry()
    store = _store(tmp_path)
    activity = [0, 0]

    async def sessions(authority_id: AuthorityId) -> PortalSession:
        if authority_id == AuthorityId("opdc"):
            raise RuntimeError("isolated fixture failure")
        return _TrackingSession(FIXTURE_BUILDERS[authority_id](WINDOW), activity)

    results = asyncio.run(
        CollectionOrchestrator(registry, store).collect(
            registry.ids(),
            WINDOW,
            sessions,
            fixture=True,
        )
    )
    assert len(results) == 15
    assert (
        sum(result.status == AuthorityCollectionStatus.SUCCEEDED for result in results)
        == 14
    )
    assert (
        sum(result.status == AuthorityCollectionStatus.FAILED for result in results)
        == 1
    )
    assert activity[1] == 4
    store.close()


def test_orchestration_success_unavailable_failure_close_and_cancel(
    tmp_path: Path,
) -> None:
    """Each authority result is isolated and sessions always close."""
    unavailable_store = _store(tmp_path / "unavailable")
    unavailable = CollectionOrchestrator(pilot_registry(), unavailable_store)

    async def should_not_run(authority_id: AuthorityId) -> PortalSession:
        raise AssertionError(authority_id)

    unavailable_result = asyncio.run(
        unavailable.collect(
            (AuthorityId("barnet"),),
            WINDOW,
            should_not_run,
            fixture=False,
        )
    )[0]
    assert unavailable_result.status == AuthorityCollectionStatus.UNAVAILABLE
    unavailable_store.close()

    registry = _registry(_live_status())
    store = _store(tmp_path / "success")
    orchestrator = CollectionOrchestrator(registry, store, max_concurrency=4)

    async def fixtures(authority_id: AuthorityId) -> PortalSession:
        assert authority_id == AuthorityId("barnet")
        return fixture_session(WINDOW)

    succeeded = asyncio.run(
        orchestrator.collect(
            (AuthorityId("barnet"),),
            WINDOW,
            fixtures,
            fixture=False,
        )
    )[0]
    assert succeeded.status == AuthorityCollectionStatus.SUCCEEDED
    assert len(succeeded.applications) == 1

    async def factory_failure(authority_id: AuthorityId) -> PortalSession:
        raise RuntimeError(authority_id)

    failed = asyncio.run(
        orchestrator.collect(
            (AuthorityId("barnet"),),
            WINDOW,
            factory_failure,
            fixture=False,
        )
    )[0]
    assert failed.status == AuthorityCollectionStatus.FAILED
    assert failed.failure_code == "RuntimeError"

    async def close_failure(authority_id: AuthorityId) -> PortalSession:
        return _CloseFailureSession(fixture_session(WINDOW))

    close_failed = asyncio.run(
        orchestrator.collect(
            (AuthorityId("barnet"),),
            WINDOW,
            close_failure,
            fixture=False,
        )
    )[0]
    assert close_failed.failure_code == "RuntimeError"

    cancelled_session = _CancelledSession()

    async def cancelled(authority_id: AuthorityId) -> PortalSession:
        return cancelled_session

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            orchestrator.collect(
                (AuthorityId("barnet"),),
                WINDOW,
                cancelled,
                fixture=False,
            )
        )
    assert cancelled_session.closed

    async def cancelled_factory(authority_id: AuthorityId) -> PortalSession:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            orchestrator.collect(
                (AuthorityId("barnet"),),
                WINDOW,
                cancelled_factory,
                fixture=False,
            )
        )
    store.close()

    zero_store = _store(tmp_path / "zero")
    five_store = _store(tmp_path / "five")
    try:
        with pytest.raises(ValueError, match="between one and four"):
            CollectionOrchestrator(registry, zero_store, max_concurrency=0)
        with pytest.raises(ValueError, match="between one and four"):
            CollectionOrchestrator(registry, five_store, max_concurrency=5)
    finally:
        zero_store.close()
        five_store.close()


def test_live_session_factory_selects_typed_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the status transport discriminator chooses HTTP or browser."""
    http_factory = LiveSessionFactory(_registry(_live_status(LiveTransportKind.HTTP)))
    http_session = asyncio.run(http_factory(AuthorityId("barnet")))
    assert isinstance(http_session, HttpxPortalSession)
    asyncio.run(http_session.aclose())

    browser_session = fixture_session(WINDOW)
    create = AsyncMock(return_value=browser_session)
    monkeypatch.setattr(PlaywrightPortalSession, "create", create)
    browser_factory = LiveSessionFactory(
        _registry(_live_status(LiveTransportKind.BROWSER))
    )
    assert asyncio.run(browser_factory(AuthorityId("barnet"))) is browser_session
    create.assert_awaited_once()

    missing_factory = LiveSessionFactory(_registry(_live_status(None)))
    with pytest.raises(ValueError, match="no live transport"):
        asyncio.run(missing_factory(AuthorityId("barnet")))


class _FakeSurface:
    def __init__(self, query: str = "") -> None:
        self.query = query
        self.metrics: list[tuple[str, object]] = []
        self.tables: list[object] = []
        self.maps: list[object] = []
        self.titles: list[str] = []

    def title(self, body: str) -> None:
        self.titles.append(body)

    def subheader(self, body: str) -> None:
        self.titles.append(body)

    def metric(self, label: str, value: object) -> None:
        self.metrics.append((label, value))

    def dataframe(self, data: object, *, use_container_width: bool) -> None:
        assert use_container_width
        self.tables.append(data)

    def text_input(self, label: str) -> str:
        assert label == "Search applications"
        return self.query

    def map(self, data: object) -> None:
        self.maps.append(data)


def _empty_snapshot() -> DashboardSnapshot:
    return DashboardSnapshot(
        coverage_implemented=15,
        coverage_denominator=15,
        live_ready=0,
        live_readiness_denominator=15,
        authorities=(),
        request_count=0,
        transferred_bytes=0,
        duration_ms=0,
        browser_time_ms=0,
        storage_growth_bytes=0,
        application_count=0,
        observed_change_count=0,
        unmapped_count=0,
    )


def test_streamlit_renderer_model_launcher_and_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI is real while JSON-free rendering stays deterministic."""
    surface = _FakeSurface()
    hit = ApplicationSearchHit(
        application_id=ApplicationId("app-1"),
        authority_id=AuthorityId("barnet"),
        reference="REF/1",
        proposal="Test",
        address="1 Test Street",
        location=ApplicationLocation(
            bng_easting=530000,
            bng_northing=180000,
            wgs84=Wgs84Coordinate(longitude=-0.1, latitude=51.5),
        ),
    )
    render_dashboard(surface, _empty_snapshot(), (hit,))
    assert ("Live ready", "0/15") in surface.metrics
    assert surface.maps == [[{"lat": 51.5, "lon": -0.1}]]

    no_locations = _FakeSurface()
    render_dashboard(no_locations, _empty_snapshot(), ())
    assert no_locations.maps == []

    queried = _FakeSurface("missing")
    run_dashboard(tmp_path / "dashboard", queried)
    assert len(queried.tables) == 2

    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], *, check: bool) -> SimpleNamespace:
        assert not check
        commands.append(command)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("yimby.dashboard_app.subprocess.run", fake_run)
    assert launch_dashboard(tmp_path) == 7
    assert commands[0][1:4] == ("-m", "streamlit", "run")

    entry_surface = _FakeSurface()
    monkeypatch.setattr(
        "yimby.dashboard_app.import_module",
        lambda _name: cast("DashboardSurface", entry_surface),
    )
    assert main(("--data-dir", str(tmp_path / "entry"))) == 0
    assert entry_surface.titles


def test_cli_streamlit_launch_and_overlap_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dashboard launches by default and active collection locks fail safely."""
    monkeypatch.setattr("yimby.cli.launch_dashboard", lambda _path: 9)
    assert cli_main(("--data-dir", str(tmp_path), "dashboard")) == 9

    lock = tmp_path / "collection.lock"
    with ProcessLock(lock):
        assert (
            cli_main(
                (
                    "--data-dir",
                    str(tmp_path),
                    "sync",
                    "--authority",
                    "barnet",
                    "--fixture",
                )
            )
            == 2
        )
    assert "collection already running" in capsys.readouterr().err
