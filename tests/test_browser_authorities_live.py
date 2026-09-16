# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, E501, PLR2004, SLF001

"""Live boundaries for Cheshire East and Haringey's browser register."""

from __future__ import annotations

import asyncio
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import HttpUrl

import yimby.authorities.cheshire_east.adapter as cheshire
import yimby.authorities.haringey.adapter as haringey
import yimby.authorities.haringey.page_object as haringey_page
from yimby import AuthorityId, Collector, DiscoveryWindow
from yimby.adapters import AuthorityPackage
from yimby.authorities.haringey.page_object import HaringeyPlaywrightSession
from yimby.browser_transport import (
    BrowserPayload,
    PlaywrightBoundary,
    PlaywrightPortalSession,
)
from yimby.domain import (
    DiscoveryBatch,
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.registry import AuthorityRegistry, pilot_registry
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    FixtureResponse,
    FixtureSession,
    PortalRequest,
    RequestIntent,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from playwright.async_api import Page

TODAY = date(2026, 9, 16)
RECENT = DiscoveryWindow(start=date(2026, 9, 10), end=TODAY, include_open=False)


def _capture(url: str, body: bytes) -> EvidenceCapture:
    return EvidenceCapture(
        url=HttpUrl(url),
        media_type="text/html",
        body=body,
        digest=EvidenceDigest(sha256(body).hexdigest()),
    )


class _HttpSession:
    def __init__(self, responder: Callable[[PortalRequest], bytes]) -> None:
        self.responder = responder
        self.requests: list[PortalRequest] = []
        self._bytes = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        body = self.responder(request)
        self._bytes += len(body)
        return _capture(str(request.url), body)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(str(request.url) for request in self.requests)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return self._bytes

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.LIVE

    async def aclose(self) -> None:
        return None


def _cheshire_form() -> bytes:
    return b"""
    <form id="form" name="form" method="post" action="/planning/index.html">
      <input type="hidden" name="fa" value="search">
      <input type="hidden" name="submitted" value="">
      <input name="application_reference_number" value="">
      <input name="valid_date_from" value="">
      <input name="valid_date_to" value="">
      <input type="submit" name="search" value="Search">
      <button type="submit">Search</button>
    </form>
    """


def _cheshire_results() -> bytes:
    return b"""
    <table id="application_results_table">
      <tr><th>Reference</th><th>Application Type</th><th>Location</th>
      <th>Proposal</th><th>Ward</th><th>Community</th>
      <th>Consultation closes</th><th>Decision</th><th>View</th></tr>
      <tr><td>26/3322/NMA</td><td>Non-material amendment</td>
      <td>One High Street</td><td>Move one window &amp; door</td><td>Ward</td>
      <td>Community</td><td>30/09/2026</td><td></td>
      <td><button class="view_application" data-id="987654">View</button></td></tr>
      <tr><td>26/3335/PRIOR-1A</td><td>Prior approval</td>
      <td>Two High Street</td><td>Convert upper floor</td><td>Ward</td>
      <td>Community</td><td></td><td></td>
      <td><button class="view_application" data-id="987655">View</button></td></tr>
    </table>
    """


class _CheshireResponder:
    def __call__(self, request: PortalRequest) -> bytes:
        if len(self.calls) == 0:
            self.calls.append(request)
            return _cheshire_form()
        self.calls.append(request)
        fields = tuple((field.name, field.value) for field in request.form)
        assert fields == (
            ("fa", "search"),
            ("submitted", ""),
            ("application_reference_number", ""),
            ("valid_date_from", "10/09/2026"),
            ("valid_date_to", ""),
        )
        return _cheshire_results()

    def __init__(self) -> None:
        self.calls: list[PortalRequest] = []


def test_cheshire_live_discovery_stops_before_source_io() -> None:
    """An unproved source inventory cannot create partial durable work."""
    responder = _CheshireResponder()
    session = _HttpSession(responder)
    adapter = cheshire.CheshireEastAdapter()

    async def exercise() -> None:
        discovery = adapter.discover(session, RECENT, None)
        with pytest.raises(cheshire.CheshireEastResultCompletenessUnavailableError):
            await anext(discovery)
        assert session.requests == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"<html></html>", cheshire.CheshireEastParseError),
        (
            b'<form id="form" name="form" method="get"></form>',
            cheshire.CheshireEastFormMethodUnavailableError,
        ),
        (
            b'<form id="form" name="wrong" method="post"></form>',
            cheshire.CheshireEastParseError,
        ),
        (
            b'<form id="form" name="form" method="post"></form>',
            cheshire.CheshireEastParseError,
        ),
    ],
)
def test_cheshire_form_boundary_failures(body: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        cheshire.parse_search_form(body)


def test_cheshire_result_and_checkpoint_boundaries() -> None:
    """Malformed rows and cross-window checkpoints fail without detail guesses."""
    for body in (
        b"<html></html>",
        b'<table id="application_results_table"></table>',
        b'<table id="application_results_table"><tr><th>Reference</th></tr></table>',
        b'<table id="application_results_table"><tr><th>Reference</th></tr><tr><td>A</td><td>B</td></tr></table>',
        b'<table id="application_results_table"><tr><th>Reference</th></tr><tr><td>A</td></tr></table>',
    ):
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire._parse_result_table(body)
    stale = cheshire.CheshireEastCheckpointV1(
        search_page="live", window_start=date(2026, 1, 1), window_end=date(2026, 1, 2)
    )
    with pytest.raises(cheshire.CheshireEastCheckpointError):
        cheshire._assert_window(stale, RECENT)
    assert cheshire._optional_mapping({"optional": ""}, "missing") is None
    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire._required_mapping({}, "reference")


def _search_hit(number: int) -> haringey.HaringeySearchHitV1:
    reference = f"HGY/2026/{number:04d}"
    return haringey.HaringeySearchHitV1(
        record_id=f"a0iP{number:08d}",
        public_reference=reference,
        address=f"{number} High Road",
        proposal=f"Proposal {number}",
        valid_date=TODAY,
        status="Pending Consideration",
        detail_url=HttpUrl(
            f"https://londonboroughofharingey.my.site.com/pr/s/detail/a0iP{number:08d}"
        ),
    )


def _search_page(
    page: int,
    hits: tuple[haringey.HaringeySearchHitV1, ...],
    *,
    pages: int,
    total: int,
) -> haringey.HaringeySearchPageV1:
    return haringey.HaringeySearchPageV1(
        register_name=haringey.REGISTER_NAME,
        quick_link_name=haringey.QUICK_LINK_NAME,
        encoded_query="c2FuaXRpc2Vk",
        page_number=page,
        reported_page_count=pages,
        reported_result_count=total,
        hits=hits,
        evidence=_capture(f"https://example.test/register/page/{page}", b"rendered"),
    )


def _detail_html(reference: str) -> bytes:
    return f"""
    <dl>
      <dt>Reference</dt><dd>{reference}</dd>
      <dt>Application Type</dt><dd>Full planning permission</dd>
      <dt>Address</dt><dd>1 High Road</dd>
      <dt>Proposal</dt><dd>Build &amp; landscape six homes</dd>
      <dt>Status</dt><dd>Pending Consideration</dd>
      <dt>Officer</dt><dd>Officer One</dd>
      <dt>Determination Level</dt><dd>Delegated</dd>
      <dt>Ward</dt><dd>Woodside</dd>
      <dt>Applicant</dt><dd>Applicant One</dd>
      <dt>Agent</dt><dd>Agent One</dd>
      <dt>Valid Date</dt><dd>16/09/2026</dd>
      <dt>Consultation End Date</dt><dd>30 September 2026</dd>
      <dt>Target Decision Date</dt><dd>2026-11-11</dd>
      <dt>Planning Portal Reference</dt><dd>PP-10001</dd>
    </dl>
    <a href="https://gis.example.test/constraints">View GIS constraints</a>
    """.encode()


def _files_html(count: int = 6, *, bad_descriptor: bool = False) -> bytes:
    rows = "".join(
        f"""
        <tr><td>{index + 1:02d}/09/2026</td><td>File {index + 1}</td>
        <td><a aria-label="{"Download" if bad_descriptor else "Download PDF 1.2 MB"}"
        href="/pr/sfc/servlet.shepherd/version/download/file-{index + 1}">Download</a></td></tr>
        """
        for index in range(count)
    )
    return f"<table><thead><tr><th>Date</th><th>Title</th><th>Download</th></tr></thead><tbody>{rows}</tbody></table>".encode()


def _application_pages(
    reference: str,
    *,
    comments: bytes = b"<p>There are no comments.</p>",
    files: bytes | None = None,
    reported_files: int = 6,
) -> haringey.HaringeyApplicationPagesV1:
    return haringey.HaringeyApplicationPagesV1(
        detail=_capture("https://example.test/detail", _detail_html(reference)),
        comments=_capture("https://example.test/comments", comments),
        files=_capture("https://example.test/files", files or _files_html()),
        reported_file_count=reported_files,
    )


class _BrowserSession:
    def __init__(
        self,
        pages: dict[int, haringey.HaringeySearchPageV1],
        *,
        application_factory: Callable[
            [str], haringey.HaringeyApplicationPagesV1
        ] = _application_pages,
    ) -> None:
        self.pages = pages
        self.application_factory = application_factory
        self.page_calls: list[int] = []
        self.application_calls: list[str] = []
        self._requested: list[str] = []

    async def validated_last_seven_days(
        self, page_number: int
    ) -> haringey.HaringeySearchPageV1:
        self.page_calls.append(page_number)
        self._requested.append(f"https://example.test/register/{page_number}")
        return self.pages[page_number]

    async def application_pages(
        self, locator: haringey.HaringeyLocatorV1
    ) -> haringey.HaringeyApplicationPagesV1:
        self.application_calls.append(locator.record_id)
        self._requested.extend(
            (
                f"https://example.test/detail/{locator.record_id}",
                f"https://example.test/comments/{locator.record_id}",
                f"https://example.test/files/{locator.record_id}",
            )
        )
        return self.application_factory(locator.public_reference)

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        raise AssertionError(request)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(self._requested)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return 0

    @property
    def browser_time_ms(self) -> int:
        return 1

    @property
    def mode(self) -> TransportMode:
        return TransportMode.BROWSER

    async def aclose(self) -> None:
        return None


def _haringey_package() -> AuthorityPackage[
    haringey.HaringeyApplicationV1, haringey.HaringeyCheckpointV1
]:
    return AuthorityPackage(
        haringey.HaringeyAdapter(today=lambda: TODAY),
        haringey.HaringeyApplicationV1,
        haringey.HaringeyCheckpointV1,
    )


def test_haringey_public_collector_keeps_files_metadata_only(tmp_path: Path) -> None:
    """The public collector stores six file rows and no attachment body request."""
    package = _haringey_package()
    store = SqliteStore(tmp_path / "db.sqlite3", EvidenceStore(tmp_path / "evidence"))
    collector = Collector(AuthorityRegistry((package,)), store)
    page = _search_page(1, (_search_hit(2582),), pages=1, total=1)
    first_session = _BrowserSession({1: page})
    second_session = _BrowserSession({1: page})

    first = asyncio.run(
        collector.collect(AuthorityId("haringey"), RECENT, first_session)
    )
    second = asyncio.run(
        collector.collect(AuthorityId("haringey"), RECENT, second_session)
    )

    assert len(first.applications) == 1
    assert second.applications == ()
    stored = store.get_application(first.applications[0])
    assert stored.proposal == "Build & landscape six homes"
    assert stored.completeness.documents.kind == "complete"
    assert stored.completeness.documents.item_count == 6
    assert stored.completeness.comments.kind == "empty"
    assert len(stored.documents) == 6
    assert store.semantic_version_count(first.applications[0], "application") == 1
    assert all("version/download" not in url for url in first.requested_urls)
    assert first.attachment_body_requests == 0
    assert first_session.application_calls == ["a0iP00002582"]
    store.close()


def test_haringey_reconciles_dynamic_six_page_capture_and_locators() -> None:
    """Dynamic totals, not the older 72/8 capture, drive traversal."""
    pages = {
        page: _search_page(
            page,
            tuple(
                _search_hit(number)
                for number in range((page - 1) * 10 + 1, min(page * 10, 59) + 1)
            ),
            pages=6,
            total=59,
        )
        for page in range(1, 7)
    }
    session = _BrowserSession(pages)
    adapter = haringey.HaringeyAdapter(today=lambda: TODAY)

    async def exercise() -> tuple[DiscoveryBatch[Any], ...]:
        return tuple([batch async for batch in adapter.discover(session, RECENT, None)])

    batches = asyncio.run(exercise())
    references = tuple(reference for batch in batches for reference in batch.references)
    assert session.page_calls == [1, 2, 3, 4, 5, 6]
    assert len(references) == 59
    assert [batch.complete for batch in batches] == [False] * 5 + [True]
    assert batches[-1].next_checkpoint.observed_result_count == 59
    encoded_locator = references[-1].locator
    assert encoded_locator is not None
    locator = haringey.HaringeyLocatorV1.model_validate_json(encoded_locator)
    assert (locator.result_page, locator.record_id) == (6, "a0iP00000059")
    assert locator.encoded_query == "c2FuaXRpc2Vk"


def test_haringey_discovery_failure_boundaries() -> None:
    """Wrong windows, pages, totals, and open scope fail explicitly."""
    adapter = haringey.HaringeyAdapter(today=lambda: TODAY)
    hit = _search_hit(1)

    async def first_error(
        session: Any,
        window: DiscoveryWindow = RECENT,
        checkpoint: haringey.HaringeyCheckpointV1 | None = None,
    ) -> None:
        discovery = adapter.discover(session, window, checkpoint)
        await anext(discovery)

    plain = _HttpSession(lambda _request: b"")
    with pytest.raises(haringey.HaringeyBrowserSessionRequiredError):
        asyncio.run(first_error(plain))
    with pytest.raises(haringey.HaringeyWindowUnavailableError):
        asyncio.run(
            first_error(
                _BrowserSession({1: _search_page(1, (hit,), pages=1, total=1)}),
                DiscoveryWindow(start=date(2026, 9, 9), end=TODAY, include_open=False),
            )
        )
    stale = haringey.HaringeyCheckpointV1(
        window_start=date(2026, 1, 1), window_end=date(2026, 1, 7)
    )
    with pytest.raises(haringey.HaringeyCheckpointError):
        asyncio.run(
            first_error(
                _BrowserSession({1: _search_page(1, (hit,), pages=1, total=1)}),
                checkpoint=stale,
            )
        )
    wrong_page = _search_page(2, (hit,), pages=2, total=11)
    with pytest.raises(haringey.HaringeyPageMismatchError):
        asyncio.run(first_error(_BrowserSession({1: wrong_page})))
    empty = _search_page(1, (), pages=2, total=1)
    with pytest.raises(haringey.HaringeyEmptyIntermediatePageError):
        asyncio.run(first_error(_BrowserSession({1: empty})))
    mismatch = _search_page(1, (hit,), pages=1, total=2)
    with pytest.raises(haringey.HaringeyResultCountMismatchError):
        asyncio.run(first_error(_BrowserSession({1: mismatch})))
    changed = haringey.HaringeyCheckpointV1(
        window_start=RECENT.start,
        window_end=RECENT.end,
        reported_page_count=2,
        reported_result_count=2,
    )
    with pytest.raises(haringey.HaringeyReportedTotalsChangedError):
        asyncio.run(
            first_error(
                _BrowserSession({1: _search_page(1, (hit,), pages=1, total=1)}),
                checkpoint=changed,
            )
        )
    open_window = RECENT.model_copy(update={"include_open": True})

    async def exhaust_open() -> None:
        async for _batch in adapter.discover(
            _BrowserSession({1: _search_page(1, (hit,), pages=1, total=1)}),
            open_window,
            None,
        ):
            pass

    with pytest.raises(haringey.HaringeyOlderOpenUnavailableError):
        asyncio.run(exhaust_open())


def test_haringey_terminal_and_deduplicated_resume() -> None:
    """A completed checkpoint is idempotent and repeated rows stay source-local."""
    adapter = haringey.HaringeyAdapter(today=lambda: TODAY)
    hit = _search_hit(1)
    checkpoint = haringey.HaringeyCheckpointV1(
        page_token="complete",  # noqa: S106 - checkpoint cursor.
        window_start=RECENT.start,
        window_end=RECENT.end,
        next_page=2,
        reported_page_count=1,
        reported_result_count=1,
        observed_result_count=1,
        seen_references=(hit.public_reference,),
        quick_link_complete=True,
    )
    session = _BrowserSession({})

    async def complete() -> Any:
        return await anext(adapter.discover(session, RECENT, checkpoint))

    batch = asyncio.run(complete())
    assert batch.complete
    assert batch.references == ()

    async def open_terminal() -> Any:
        return await anext(
            adapter.discover(
                session,
                RECENT.model_copy(update={"include_open": True}),
                checkpoint,
            )
        )

    with pytest.raises(haringey.HaringeyOlderOpenUnavailableError):
        asyncio.run(open_terminal())

    duplicate_checkpoint = checkpoint.model_copy(
        update={
            "quick_link_complete": False,
            "next_page": 1,
            "reported_result_count": 2,
        }
    )

    async def duplicate() -> Any:
        return await anext(
            adapter.discover(
                _BrowserSession({1: _search_page(1, (hit,), pages=1, total=2)}),
                RECENT,
                duplicate_checkpoint,
            )
        )

    batch = asyncio.run(duplicate())
    assert batch.references == ()


def test_haringey_fetch_failure_sections_and_routing() -> None:
    """Child tab failures remain explicit while a valid detail survives."""
    adapter = haringey.HaringeyAdapter(today=lambda: TODAY)
    hit = _search_hit(2582)
    page = _search_page(1, (hit,), pages=1, total=1)

    async def reference() -> Any:
        return (
            await anext(adapter.discover(_BrowserSession({1: page}), RECENT, None))
        ).references[0]

    routed = asyncio.run(reference())
    failed = _BrowserSession(
        {1: page},
        application_factory=lambda ref: _application_pages(
            ref,
            comments=b"<p>Comments could not load</p>",
            files=_files_html(bad_descriptor=True),
        ),
    )
    snapshot = asyncio.run(adapter.fetch(failed, routed))
    assert snapshot.completeness.application.kind == "complete"
    assert snapshot.completeness.documents.kind == "failed"
    assert snapshot.completeness.comments.kind == "failed"
    assert snapshot.payload.files == ()

    mismatch = _BrowserSession(
        {1: page}, application_factory=lambda _ref: _application_pages("HGY/WRONG")
    )
    with pytest.raises(haringey.HaringeyReferenceMismatchError):
        asyncio.run(adapter.fetch(mismatch, routed))
    for bad in (
        SourceReference(source_id=haringey.SOURCE, reference="HGY/1"),
        SourceReference(
            source_id=haringey.SOURCE, reference="HGY/1", locator="not-json"
        ),
    ):
        with pytest.raises(haringey.HaringeyRoutingError):
            asyncio.run(adapter.fetch(_BrowserSession({}), bad))
    with pytest.raises(haringey.HaringeyBrowserSessionRequiredError):
        asyncio.run(adapter.fetch(_HttpSession(lambda _request: b""), routed))


def test_haringey_detail_and_file_parser_boundaries() -> None:
    """Detail labels, date formats, and accessible file descriptions are strict."""
    fields, constraint = haringey._parse_detail(_detail_html("HGY/2026/2582"))
    assert fields["proposal"] == "Build & landscape six homes"
    assert str(constraint) == "https://gis.example.test/constraints"
    assert haringey._parse_date("16 Sep 2026") == date(2026, 9, 16)
    with pytest.raises(ValueError, match="not-a-date"):
        haringey._parse_date("not-a-date")
    with pytest.raises(haringey.HaringeyParseError):
        haringey._required_date({"valid date": "bad"}, "valid date")
    for body in (
        b"<dl><dt>Reference</dt></dl>",
        _detail_html("HGY/1").replace(b"View GIS constraints", b"Map"),
    ):
        with pytest.raises(haringey.HaringeyParseError):
            haringey._parse_detail(body)
    for body in (
        b"<html></html>",
        b"<table><thead><tr><th>Date</th><th>Title</th><th>Download</th></tr></thead><tbody><tr><td>x</td></tr></tbody></table>",
        b"<table><thead><tr><th>Date</th><th>Title</th><th>Download</th></tr></thead><tbody><tr><td>01/09/2026</td><td>X</td><td>none</td></tr></tbody></table>",
    ):
        with pytest.raises(haringey.HaringeyParseError):
            haringey._parse_files(body)
    prefixed = b"<table><tr><td>unrelated</td></tr></table>" + _files_html(1)
    assert len(haringey._parse_files(prefixed)) == 1
    with pytest.raises(haringey.HaringeyParseError):
        haringey._file_size("Download PDF")
    state = haringey._files_or_failure(_files_html(1), 2)[1]
    assert state.kind == "failed"
    assert state.code == "file-count-mismatch"


class _InteractiveBoundary:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.closed = False

    async def open(self, url: str) -> BrowserPayload:
        return BrowserPayload(body=url.encode(), media_type="text/html")

    async def interact(self, operation: Any) -> Any:
        return await operation(self.page)

    async def aclose(self) -> None:
        self.closed = True


def _card_mock(hit: haringey.HaringeySearchHitV1) -> MagicMock:
    card = MagicMock()
    card.inner_text = AsyncMock(
        return_value=(
            "Application Reference\n"
            f"{hit.public_reference}\n"
            "Site Address\n"
            f"{hit.address}\n"
            "Proposal\n"
            f"{hit.proposal}\n"
            "Date Valid\n"
            "16/09/2026\n"
            "Application Status\n"
            f"{hit.status}\n"
        )
    )
    link = MagicMock()
    link.get_attribute = AsyncMock(return_value=f"/pr/s/detail/{hit.record_id}")
    locator = MagicMock()
    locator.first = link
    card.locator.return_value = locator
    return card


def _search_page_mock(*, malformed_route: bool = False) -> MagicMock:
    page = MagicMock()
    page.url = (
        "https://londonboroughofharingey.my.site.com/pr/s/register-view"
        f"?c__r={'Wrong' if malformed_route else haringey.REGISTER_NAME}&c__q=query"
    )
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.content = AsyncMock(return_value="<html>rendered search</html>")
    selection_order: list[str] = []
    register_button = MagicMock()
    register_button.click = AsyncMock(
        side_effect=lambda: selection_order.append("register")
    )
    quick_link_button = MagicMock()
    quick_link_button.click = AsyncMock(
        side_effect=lambda: selection_order.append("quick-link")
    )
    next_set = MagicMock()
    next_set.count = AsyncMock(return_value=1)
    next_set.click = AsyncMock()

    def role(role_name: str, **kwargs: Any) -> MagicMock:
        if role_name != "button":
            return next_set
        if kwargs.get("name") == "Haringey Public Register":
            return register_button
        return quick_link_button

    page.get_by_role.side_effect = role
    page.selection_order = selection_order
    page.register_button = register_button
    page.quick_link_button = quick_link_button
    result_count = MagicMock()
    result_count.inner_text = AsyncMock(
        side_effect=("Showing 1 to 10 of 11 results", "Showing 11 to 11 of 11 results")
    )
    target = MagicMock()
    target.count = AsyncMock(return_value=1)
    target.click = AsyncMock()
    hit = _search_hit(11)
    card = _card_mock(hit)
    cards = MagicMock()
    cards.count = AsyncMock(return_value=1)
    cards.nth.return_value = card

    def locator(selector: str) -> MagicMock:
        if selector == haringey_page._RESULT_COUNT:
            return result_count
        if selector == haringey_page._RESULT_CARD:
            return cards
        return target

    page.locator.side_effect = locator
    return page


def test_haringey_page_object_uses_recorded_search_selectors() -> None:
    """The production page object drives the exact quick-link and page selector."""
    page = _search_page_mock()
    boundary = _InteractiveBoundary(cast("Page", page))
    clock = iter((1.0, 1.1))
    session = HaringeyPlaywrightSession(boundary, clock=lambda: next(clock))

    result = asyncio.run(session.validated_last_seven_days(2))

    assert result.page_number == 2
    assert result.reported_result_count == 11
    assert result.hits[0].record_id == "a0iP00000011"
    assert page.selection_order == ["register", "quick-link"]
    page.register_button.click.assert_awaited_once_with()
    page.quick_link_button.click.assert_awaited_once_with()
    page.get_by_role.assert_any_call(
        "button", name="Haringey Public Register", exact=True
    )
    page.get_by_role.assert_any_call(
        "button", name="Planning Applications Validated in last 7 days", exact=True
    )
    page.locator.assert_any_call('a.pr-pagination__link[data-id="2"]')
    assert session.requested_urls == (
        "https://londonboroughofharingey.my.site.com/pr/s/register-view",
    )
    assert session.browser_time_ms == 100


def _detail_page_mock() -> MagicMock:
    page = MagicMock()
    page.url = "https://example.test/planning-application/a0iP00002582/example"
    page.goto = AsyncMock()
    interaction_order: list[str] = []
    rendered = iter(
        (
            ("detail-content", _detail_html("HGY/2026/2582").decode()),
            ("comments-content", "<p>There are no comments.</p>"),
            ("files-content", _files_html().decode()),
        )
    )

    def content() -> str:
        event, body = next(rendered)
        interaction_order.append(event)
        return body

    page.content = AsyncMock(side_effect=content)
    heading = MagicMock()
    heading.wait_for = AsyncMock()
    comments_tab = MagicMock()
    comments_tab.click = AsyncMock(
        side_effect=lambda: interaction_order.append("comments-click")
    )
    files_tab = MagicMock()
    files_tab.click = AsyncMock(
        side_effect=lambda: interaction_order.append("files-click")
    )
    empty = MagicMock()
    empty.wait_for = AsyncMock(
        side_effect=lambda: interaction_order.append("comments-wait")
    )
    table = MagicMock()
    table.wait_for = AsyncMock()
    rows = MagicMock()
    rows.count = AsyncMock(return_value=7)
    table.get_by_role.return_value = rows

    def role(role_name: str, **kwargs: Any) -> MagicMock:
        if role_name == "heading":
            return heading
        if role_name == "tab":
            return comments_tab if kwargs.get("name") == "Comments" else files_tab
        return table

    page.get_by_role.side_effect = role
    page.get_by_text.return_value = empty
    page.interaction_order = interaction_order
    return page


def test_haringey_page_object_opens_only_recorded_child_tabs() -> None:
    """Detail capture clicks Comments and Files but no file or Download all control."""
    page = _detail_page_mock()
    boundary = _InteractiveBoundary(cast("Page", page))
    session = HaringeyPlaywrightSession(boundary)
    locator = haringey.HaringeyLocatorV1(
        register_name=haringey.REGISTER_NAME,
        quick_link_name=haringey.QUICK_LINK_NAME,
        encoded_query="query",
        result_page=1,
        record_id="a0iP00002582",
        public_reference="HGY/2026/2582",
        detail_url=HttpUrl("https://example.test/pr/s/detail/a0iP00002582"),
    )

    pages = asyncio.run(session.application_pages(locator))

    assert pages.reported_file_count == 6
    page.get_by_role.assert_any_call("heading", name="HGY/2026/2582", exact=True)
    page.get_by_role.assert_any_call("tab", name="Comments", exact=True)
    page.get_by_role.assert_any_call("tab", name="Files", exact=True)
    page.get_by_text.assert_called_once_with("There are no comments.", exact=True)
    page.get_by_text.return_value.wait_for.assert_awaited_once_with()
    assert page.interaction_order == [
        "detail-content",
        "comments-click",
        "comments-wait",
        "comments-content",
        "files-click",
        "files-content",
    ]
    assert all(
        "download" not in call.kwargs.get("name", "").casefold()
        for call in page.get_by_role.mock_calls
    )
    assert len(session.requested_urls) == 3


def test_page_object_parser_and_pagination_failures() -> None:
    """Selector drift and malformed visible values fail at the page-object boundary."""
    for value in (
        "bad",
        "Showing 0 to 1 of 1 results",
        "Showing 2 to 1 of 1 results",
        "Showing 1 to 2 of 1 results",
    ):
        with pytest.raises(haringey_page.HaringeyPageObjectParseError):
            haringey_page._parse_result_range(value)
    with pytest.raises(haringey_page.HaringeyPageObjectPaginationError):
        haringey_page._assert_range(haringey_page._ResultRange(1, 1, 11), 2)
    with pytest.raises(haringey_page.HaringeyPageObjectParseError):
        haringey_page._parse_card_text("Application Reference\nHGY/1")
    with pytest.raises(haringey_page.HaringeyPageObjectParseError):
        haringey_page._record_id("/wrong/path")
    with pytest.raises(haringey_page.HaringeyPageObjectParseError):
        haringey_page._parse_card_date("bad")
    malformed = _search_page_mock(malformed_route=True)
    session = HaringeyPlaywrightSession(_InteractiveBoundary(cast("Page", malformed)))
    with pytest.raises(haringey_page.HaringeyPageObjectRouteError):
        asyncio.run(session.validated_last_seven_days(1))


def test_haringey_page_object_search_failure_branches() -> None:
    """Page bounds, card counts, and missing result links are explicit failures."""

    async def validate(page: MagicMock, number: int) -> Any:
        session = HaringeyPlaywrightSession(_InteractiveBoundary(cast("Page", page)))
        return await session.validated_last_seven_days(number)

    with pytest.raises(haringey_page.HaringeyPageObjectPaginationError):
        asyncio.run(validate(_search_page_mock(), 0))
    with pytest.raises(haringey_page.HaringeyPageObjectPaginationError):
        asyncio.run(validate(_search_page_mock(), 3))

    successful = _search_page_mock()
    successful.locator(haringey_page._RESULT_COUNT).inner_text = AsyncMock(
        side_effect=("Showing 1 to 1 of 1 results", "Showing 1 to 1 of 1 results")
    )
    result = asyncio.run(validate(successful, 1))
    assert result.page_number == 1

    count_mismatch = _search_page_mock()
    count_mismatch.locator(haringey_page._RESULT_COUNT).inner_text = AsyncMock(
        side_effect=("Showing 1 to 1 of 1 results", "Showing 1 to 1 of 1 results")
    )
    count_mismatch.locator(haringey_page._RESULT_CARD).count = AsyncMock(return_value=0)
    with pytest.raises(haringey_page.HaringeyPageObjectCountError):
        asyncio.run(validate(count_mismatch, 1))

    missing_link = _search_page_mock()
    missing_link.locator(haringey_page._RESULT_COUNT).inner_text = AsyncMock(
        side_effect=("Showing 1 to 1 of 1 results", "Showing 1 to 1 of 1 results")
    )
    card = missing_link.locator(haringey_page._RESULT_CARD).nth(0)
    card.locator(haringey_page._RESULT_LINK).first.get_attribute = AsyncMock(
        return_value=None
    )
    with pytest.raises(haringey_page.HaringeyPageObjectParseError):
        asyncio.run(validate(missing_link, 1))


def test_haringey_page_selection_sets_and_missing_files_header() -> None:
    """Component-local page sets are bounded and a files header is mandatory."""
    page = MagicMock()
    target = MagicMock()
    target.count = AsyncMock(side_effect=(0, 1))
    target.click = AsyncMock()
    next_set = MagicMock()
    next_set.count = AsyncMock(return_value=1)
    next_set.click = AsyncMock()
    page.locator.return_value = target
    page.get_by_role.return_value = next_set
    page.wait_for_function = AsyncMock()
    asyncio.run(haringey_page._select_page(cast("Page", page), 2))
    next_set.click.assert_awaited_once()

    missing = MagicMock()
    missing_target = MagicMock()
    missing_target.count = AsyncMock(return_value=0)
    missing_next = MagicMock()
    missing_next.count = AsyncMock(return_value=0)
    missing.locator.return_value = missing_target
    missing.get_by_role.return_value = missing_next
    with pytest.raises(haringey_page.HaringeyPageObjectPaginationError):
        asyncio.run(haringey_page._select_page(cast("Page", missing), 2))

    detail = _detail_page_mock()
    detail.get_by_role("table").get_by_role("row").count = AsyncMock(return_value=0)
    session = HaringeyPlaywrightSession(_InteractiveBoundary(cast("Page", detail)))
    locator = haringey.HaringeyLocatorV1(
        register_name=haringey.REGISTER_NAME,
        quick_link_name=haringey.QUICK_LINK_NAME,
        encoded_query="query",
        result_page=1,
        record_id="a0iP00002582",
        public_reference="HGY/2026/2582",
        detail_url=HttpUrl("https://example.test/pr/s/detail/a0iP00002582"),
    )
    with pytest.raises(haringey_page.HaringeyPageObjectParseError):
        asyncio.run(session.application_pages(locator))


def test_playwright_interaction_hook_lifecycle_and_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic hook owns page lifecycle without owning authority selectors."""
    page = MagicMock()
    page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    boundary = PlaywrightBoundary(playwright, browser, context)

    async def interact() -> str:
        return await boundary.interact(lambda _page: _value("done"))

    async def _value(value: str) -> str:
        return value

    assert asyncio.run(interact()) == "done"
    page.close.assert_awaited_once()

    created = _InteractiveBoundary(cast("Page", _search_page_mock()))
    monkeypatch.setattr(PlaywrightBoundary, "create", AsyncMock(return_value=created))

    async def create() -> None:
        session = await HaringeyPlaywrightSession.create()
        await session.aclose()

    asyncio.run(create())
    assert created.closed

    noninteractive = PlaywrightPortalSession(
        cast("Any", MagicMock(spec=["open", "aclose"]))
    )
    with pytest.raises(SourceUnavailableError, match="browser source unavailable"):
        asyncio.run(noninteractive.run_browser(lambda _page: _value("unused")))


def test_extensionless_salesforce_download_is_blocked_everywhere() -> None:
    """Fixture, HTTP, and browser transports reject Salesforce version bodies."""
    url = "https://example.test/pr/sfc/servlet.shepherd/version/download/123"
    request = PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
    fixture = FixtureSession({url: FixtureResponse(body=b"never")})

    class _Boundary:
        async def open(self, _url: str) -> BrowserPayload:
            raise AssertionError

        async def aclose(self) -> None:
            return None

    browser = PlaywrightPortalSession(_Boundary())
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"never")
    )
    http = HttpxPortalSession(
        client=httpx.AsyncClient(transport=transport),
        limiter=HostRateLimiter(minimum_gap=0),
    )

    async def blocked() -> None:
        for session in (fixture, browser, http):
            with pytest.raises(AttachmentBodyBlockedError):
                await session.fetch(request)
            assert session.attachment_body_requests == 1
        await http.aclose()

    asyncio.run(blocked())


def test_registry_statuses_remain_truthful() -> None:
    """Fixture and partial browser implementation never become live readiness."""
    registry = pilot_registry()
    assert (
        registry.manifest(AuthorityId("cheshire-east")).live_status.readiness.value
        == "discovery-only"
    )
    assert (
        registry.manifest(AuthorityId("haringey")).live_status.readiness.value
        == "browser-only"
    )
