# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, E501, EM101, PLR0911, PLR2004, SLF001, TRY003

"""Real HTTP contract boundaries for Arun, Devon, Camden, and Peak District."""

from __future__ import annotations

import asyncio
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

import yimby.authorities.arun.adapter as arun
import yimby.authorities.camden.adapter as camden
import yimby.authorities.devon.adapter as devon
import yimby.authorities.peak_district.adapter as peak
from yimby import AuthorityId, Collector, DiscoveryWindow
from yimby.adapters import AuthorityPackage
from yimby.domain import (
    EvidenceCapture,
    EvidenceDigest,
    SourceId,
    SourceReference,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalRequest, RequestMethod, SourceUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path


class _Session:
    def __init__(
        self,
        responder: Callable[[PortalRequest], bytes],
        *,
        mode: TransportMode = TransportMode.LIVE,
    ) -> None:
        self.responder = responder
        self.requests: list[PortalRequest] = []
        self._mode = mode
        self._bytes = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        body = self.responder(request)
        self._bytes += len(body)
        return EvidenceCapture(
            url=request.url,
            media_type="text/html",
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

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
        return self._mode

    async def aclose(self) -> None:
        return None


def _store(root: Path) -> SqliteStore:
    return SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))


def _registry(package: Any) -> AuthorityRegistry:
    return AuthorityRegistry((package,))


def _arun_form() -> bytes:
    return b"""
    <form action="planningSearch" method="post">
      <input type="text" name="reference" value="old">
      <input type="text" name="location" value="old">
      <input type="text" name="OcellaPlanningSearch.postcode" value="old">
      <input type="text" name="area" value="old">
      <input type="text" name="applicant" value="old">
      <input type="text" name="agent" value="old">
      <input type="checkbox" name="undecided" value="Y">
      <select name="type"><option value="old" selected>Old</option></select>
      <input name="receivedFrom"><input name="receivedTo">
      <input name="decidedFrom"><input name="decidedTo">
      <input type="submit" name="action" value="Search">
      <input type="submit" name="action" value="Reset">
    </form>
    """


def _arun_results(
    references: tuple[str, ...],
    reported: int,
    *,
    show_all: bool,
    fields: dict[str, str],
) -> bytes:
    rows = "".join(
        f'<tr><td><a href="planningDetails?reference={reference.replace("/", "%2F")}&amp;from=planningSearch">{reference}</a></td></tr>'
        for reference in references
    )
    controls = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in fields.items()
        if name != "action"
    )
    control = (
        '<form method="post" action="planningSearch">'
        '<input type="hidden" name="action" value="Search">'
        '<input type="hidden" name="showall" value="showall">'
        f'{controls}<input type="submit" value="Show all results"></form>'
        if show_all
        else ""
    )
    return (
        f'<p data-result-count="{reported}">{reported} records</p>'
        f"<table>{rows}</table>{control}"
    ).encode()


def _arun_detail(reference: str) -> bytes:
    return f"""
    <table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Proposal</th><td>Build &amp; landscape one home</td></tr>
      <tr><th>Status</th><td>Approved</td></tr>
      <tr><th>Parish</th><td>Bognor Regis</td></tr>
      <tr><th>Location</th><td>1 Coast Road</td></tr>
      <tr><th>Application Type</th><td>Full</td></tr>
      <tr><th>Received Date</th><td>16/08/2026</td></tr>
      <tr><th>Validated Date</th><td>17 August 2026</td></tr>
      <tr><th>Decision Date</th><td>15 Sep 2026</td></tr>
      <tr><th>Case Officer</th><td>Officer One</td></tr>
      <tr><th>Applicant</th><td>Applicant One</td></tr>
      <tr><th>Agent</th><td>Agent One</td></tr>
    </table>
    <form method="post" action="showDocuments?reference={reference}&amp;module=pl">
      <input type="submit" name="ViewDocuments" value="View Documents">
    </form>
    """.encode()


def _arun_documents(reference: str) -> bytes:
    return f"""
    <form method="post" action="showDocuments?reference={reference}&amp;module=pl&amp;filterBy=TYPE">
      <select name="selectedtype"><option value="" selected>All</option></select>
    </form>
    <table>
      <tr><th>Type</th><th></th><th>Date</th><th></th><th>Description</th></tr>
      <tr><td><a href="viewDocument?file=decision.pdf&amp;module=pl">Decision</a></td>
      <td></td><td>15/09/2026</td><td></td><td>Decision notice</td></tr>
    </table>
    """.encode()


class _ArunMock:
    references = ("BR/156/25/PL", "BR/157/25/PL")

    def __init__(
        self,
        *,
        first_reported: int = 2,
        first_show_all: bool = True,
        expanded_reported: int = 2,
        expanded_rows: tuple[str, ...] | None = None,
        mismatch_detail: bool = False,
    ) -> None:
        self.first_reported = first_reported
        self.first_show_all = first_show_all
        self.expanded_reported = expanded_reported
        self.expanded_rows = expanded_rows or self.references
        self.mismatch_detail = mismatch_detail

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if request.method == RequestMethod.GET and url.rstrip("/") == arun._SEARCH_URL:
            return _arun_form()
        if request.method == RequestMethod.POST and url.rstrip("/") == arun._SEARCH_URL:
            fields = {field.name: field.value for field in request.form}
            if fields.get("showall") == "showall":
                return _arun_results(
                    self.expanded_rows,
                    self.expanded_reported,
                    show_all=False,
                    fields=fields,
                )
            if (
                fields.get("receivedFrom") == "16-08-26"
                and fields.get("receivedTo") == "16-09-26"
                and fields.get("undecided") == ""
            ):
                return _arun_results(
                    self.references[:1],
                    self.first_reported,
                    show_all=self.first_show_all,
                    fields=fields,
                )
            return b"No applications found for entered search criteria"
        if "planningDetails" in url:
            reference = parse_qs(urlsplit(url).query)["reference"][0]
            return _arun_detail("WRONG/1" if self.mismatch_detail else reference)
        if "showDocuments" in url:
            reference = parse_qs(urlsplit(url).query)["reference"][0]
            return _arun_documents(reference)
        raise AssertionError(url)


def _devon_disclaimer(return_name: str) -> bytes:
    return f"""
    <form action="/Disclaimer/Accept?returnUrl={return_name}" method="post">
      <input type="hidden" name="verification" value="sanitised">
      <input type="submit" value="Accept">
    </form>
    """.encode()


def _devon_search(*, count: int = 1, pagination: bool = False) -> bytes:
    pager = '<nav class="pagination">next</nav>' if pagination else ""
    return f"""
    <p>Found {count} records</p>{pager}
    <dl class="searchResultsList">
      <dt>Application number</dt>
      <dd><a href="/Planning/Display/DCC/4473/2026">DCC/4473/2026</a></dd>
      <dt>Proposal</dt><dd>Upgrade recycling centre</dd>
    </dl>
    """.encode()


def _devon_detail(reference: str = "DCC/4473/2026") -> bytes:
    return f"""
    <dl class="details-grid">
      <dt>Application Number</dt><dd>{reference}</dd>
      <dt>Application Type</dt><dd>County Development</dd>
      <dt>Proposal</dt><dd>Upgrade recycling centre</dd>
      <dt>Status</dt><dd>Under Consideration</dd>
      <dt>Location</dt><dd>North Devon recycling centre</dd>
      <dt>Case Officer</dt><dd>Officer Two</dd>
      <dt>Received Date</dt><dd>20/06/2026</dd>
      <dt>Validation Date</dt><dd>21 June 2026</dd>
      <dt>Decision Date</dt><dd>2026-09-15</dd>
    </dl>
    <dl class="details-grid">
      <dt>District</dt><dd>North Devon</dd>
      <dt>Electoral Division</dt><dd>Braunton Rural</dd>
      <dt>Parish</dt><dd>Georgeham</dd>
      <dt>Applicant</dt><dd>Applicant Two</dd>
      <dt>Agent</dt><dd>Agent Two</dd>
    </dl>
    <div hidden id="documents">
      <a href="/Document/Download?record=4473&amp;plan=1&amp;image=2&amp;filename=site-plan.pdf">Site plan</a>
      <a href="/Document/Download?recordNumber=4473&amp;planId=3&amp;imageId=4">Consultation response</a>
    </div>
    """.encode()


class _DevonMock:
    def __init__(
        self,
        *,
        direct: bool = False,
        repeated_disclaimer: bool = False,
        pagination: bool = False,
        count: int = 1,
        mismatch_detail: bool = False,
    ) -> None:
        self.direct = direct
        self.repeated_disclaimer = repeated_disclaimer
        self.pagination = pagination
        self.count = count
        self.mismatch_detail = mismatch_detail
        self.protected_seen: set[str] = set()

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if request.method == RequestMethod.POST and "/Disclaimer/Accept" in url:
            if self.repeated_disclaimer:
                return _devon_disclaimer("again")
            if "search" in url:
                return _devon_search(count=self.count, pagination=self.pagination)
            return _devon_detail("WRONG/1" if self.mismatch_detail else "DCC/4473/2026")
        if "/Search/Standard" in url:
            if self.direct:
                return _devon_search(count=self.count, pagination=self.pagination)
            return _devon_disclaimer("search")
        if "/Planning/Display/" in url:
            if self.direct:
                return _devon_detail(
                    "WRONG/1" if self.mismatch_detail else "DCC/4473/2026"
                )
            return _devon_disclaimer("detail")
        raise AssertionError(url)


def _peak_search(*, reported: int = 1, row_date: str = "16/09/2026") -> bytes:
    return f"""
    <p data-result-count="{reported}">Showing 1 of {reported} entries</p>
    <table id="searchresults"><tbody><tr>
      <td>NP/DIS/0926/0917</td><td>Discharge of Conditions</td>
      <td>Landscape condition</td><td>{row_date}</td>
      <td><a href="/result/sanitised-0917">View</a></td>
    </tr></tbody></table>
    """.encode()


def _peak_detail(reference: str = "NP/DIS/0926/0917", *, loading: bool = True) -> bytes:
    sections = (
        '<section id="documents">Loading...</section><section id="comments">Loading...</section>'
        if loading
        else ""
    )
    return f"""
    <div class="dataview"><table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Description</th><td>Discharge landscape condition</td></tr>
      <tr><th>Status</th><td>Pending</td></tr>
      <tr><th>Application Type</th><td>Discharge of Conditions</td></tr>
      <tr><th>Development Address</th><td>1 Moorland Lane</td></tr>
      <tr><th>Parish</th><td>Bakewell</td></tr>
      <tr><th>Validated Date</th><td>16 Sep 2026</td></tr>
      <tr><th>Planning Portal Reference</th><td>PP-15234567</td></tr>
    </table></div>
    <a href="{peak.ASSURE_BASE}/Application/0917">Current record</a>{sections}
    """.encode()


class _PeakMock:
    def __init__(
        self,
        *,
        reported: int = 1,
        row_date: str = "16/09/2026",
        mismatch_detail: bool = False,
        loading: bool = True,
    ) -> None:
        self.reported = reported
        self.row_date = row_date
        self.mismatch_detail = mismatch_detail
        self.loading = loading

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if url == peak._WEEKLY_URL:
            return _peak_search(reported=self.reported, row_date=self.row_date)
        if "/result/" in url:
            return _peak_detail(
                "WRONG/1" if self.mismatch_detail else "NP/DIS/0926/0917",
                loading=self.loading,
            )
        raise AssertionError(url)


def _camden_form() -> bytes:
    return b"""
    <form id="searchForm" action="index.xhtml;jsessionid=sanitised">
      <input name="searchForm" value="searchForm">
      <input name="searchForm:searchTermInput:textField" value="">
      <input name="searchForm:SubmitButton:button" value="">
      <input name="javax.faces.ViewState" value="view-state">
      <input name="ignored" value="ignored">
    </form>
    """


def _camden_result(reference: str = "2026/2706/L", locator: str = "681726") -> bytes:
    return f"""
    <table><tr><td>{reference}</td><td>
      <a href="https://planningrecords.camden.gov.uk/NECSWS/Redirection/redirect.aspx?linkid=EXDC&amp;PARAM0={locator}">View</a>
    </td></tr></table>
    """.encode()


def _camden_detail(reference: str = "2026/2706/L") -> bytes:
    return f"""
    <div class="dataview"><table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Address</th><td>1 Camden Square</td></tr>
      <tr><th>Application Type</th><td>Listed Building Consent</td></tr>
      <tr><th>Development Type</th><td>Alterations</td></tr>
      <tr><th>Proposal</th><td>Repair listed townhouse</td></tr>
      <tr><th>Current Status</th><td>Decision Issued</td></tr>
      <tr><th>Applicant</th><td>Applicant Three</td></tr>
      <tr><th>Agent</th><td>Agent Three</td></tr>
      <tr><th>Ward</th><td>Camden Square</td></tr>
      <tr><th>Easting</th><td>530748</td></tr>
      <tr><th>Northing</th><td>182755</td></tr>
      <tr><th>Case Officer</th><td>Officer Three</td></tr>
    </table></div>
    """.encode()


def _camden_documents(*, reported: int = 2, rows: int = 2) -> bytes:
    values = (
        ("15/09/2026", "Decision notice", "Decision", "/inline/decision"),
        ("16 September 2026", "Site plan", "Plan", "/inline/plan"),
    )[:rows]
    rendered = "".join(
        f'<tr><td>{created}</td><td>{title}</td><td>{kind}</td><td><a href="{url}">Open</a></td></tr>'
        for created, title, kind, url in values
    )
    return f"""
    <p data-result-count="{reported}">Reported {reported} records</p>
    <table><thead><tr><th>Created Date</th><th>Title</th><th>Document Type</th><th>Source</th></tr></thead>
    <tbody>{rendered}</tbody></table>
    """.encode()


class _CamdenMock:
    def __init__(
        self,
        *,
        mismatch_detail: bool = False,
        document_failure: bool = False,
        document_reported: int = 2,
    ) -> None:
        self.mismatch_detail = mismatch_detail
        self.document_failure = document_failure
        self.document_reported = document_reported

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if (
            url.rstrip("/") == camden.SEARCH_BASE
            and request.method == RequestMethod.GET
        ):
            return _camden_form()
        if "index.xhtml" in url and request.method == RequestMethod.POST:
            fields = tuple((field.name, field.value) for field in request.form)
            assert fields == (
                ("searchForm", "searchForm"),
                ("searchForm:searchTermInput:textField", "2026/2706/L"),
                ("searchForm:SubmitButton:button", "Search"),
                ("javax.faces.ViewState", "view-state"),
            )
            return _camden_result()
        if "/Redirection/redirect.aspx" in url:
            assert parse_qs(urlsplit(url).query) == {
                "linkid": ["EXDC"],
                "PARAM0": ["681726"],
            }
            return _camden_detail("WRONG/1" if self.mismatch_detail else "2026/2706/L")
        if url.startswith(camden.DOCUMENT_BASE):
            query = parse_qs(urlsplit(url).query)["q"]
            assert query == ['recContainer:"2026/2706/L"']
            if self.document_failure:
                raise SourceUnavailableError("document service unavailable")
            return _camden_documents(reported=self.document_reported)
        raise AssertionError(url)


def test_arun_public_collector_resumes_and_is_idempotent(tmp_path: Path) -> None:
    package = AuthorityPackage(
        arun.ArunAdapter(), arun.ArunApplicationV1, arun.ArunCheckpointV1
    )
    store = _store(tmp_path)
    collector = Collector(_registry(package), store)
    window = DiscoveryWindow(
        start=date(2026, 8, 16), end=date(2026, 9, 16), include_open=False
    )
    first_session = _Session(_ArunMock())
    first = asyncio.run(collector.collect(AuthorityId("arun"), window, first_session))
    assert len(first.applications) == 2
    assert first.attachment_body_requests == 0
    stored = store.get_application(first.applications[0])
    assert stored.proposal == "Build & landscape one home"
    assert stored.completeness.documents.kind == "complete"
    assert [document.title for document in stored.documents] == ["Decision notice"]
    assert store.discovery_state(AuthorityId("arun")).queued[0].locator is not None
    repeat = asyncio.run(
        collector.collect(AuthorityId("arun"), window, _Session(_ArunMock()))
    )
    assert repeat.applications == ()
    assert repeat.requested_urls == ()
    assert store.semantic_version_count(first.applications[0], "application") == 1
    assert store.semantic_version_count(first.applications[0], "documents") == 1
    assert all("ShowFile" not in url for url in first.requested_urls)
    store.close()


def test_devon_public_collector_accepts_disclaimer_and_retains_metadata(
    tmp_path: Path,
) -> None:
    adapter = devon.DevonAdapter(today=lambda: date(2026, 9, 16))
    package = AuthorityPackage(
        adapter, devon.DevonApplicationV1, devon.DevonCheckpointV1
    )
    store = _store(tmp_path)
    collector = Collector(_registry(package), store)
    window = DiscoveryWindow(
        start=date(2026, 6, 19), end=date(2026, 9, 16), include_open=False
    )
    session = _Session(_DevonMock())
    report = asyncio.run(collector.collect(AuthorityId("devon"), window, session))
    assert len(report.applications) == 1
    stored = store.get_application(report.applications[0])
    assert sorted(item.title for item in stored.documents) == [
        "Consultation response",
        "Site plan",
    ]
    assert stored.completeness.comments.kind == "excluded"
    assert report.attachment_body_requests == 0
    assert all("Document/Download" not in url for url in report.requested_urls)
    assert (
        sum(request.method == RequestMethod.POST for request in session.requests) == 2
    )
    view = store.application_view(report.applications[0])
    assert view.metadata.address == "North Devon recycling centre"
    assert view.metadata.decision_date == date(2026, 9, 15)
    store.close()


def test_peak_district_public_collector_keeps_loading_sections_failed(
    tmp_path: Path,
) -> None:
    adapter = peak.PeakDistrictAdapter(today=lambda: date(2026, 9, 16))
    package = AuthorityPackage(
        adapter, peak.PeakDistrictApplicationV1, peak.PeakDistrictCheckpointV1
    )
    store = _store(tmp_path)
    collector = Collector(_registry(package), store)
    window = DiscoveryWindow(
        start=date(2026, 9, 10), end=date(2026, 9, 16), include_open=False
    )
    report = asyncio.run(
        collector.collect(AuthorityId("peak-district"), window, _Session(_PeakMock()))
    )
    assert len(report.applications) == 1
    stored = store.get_application(report.applications[0])
    assert stored.completeness.documents.kind == "failed"
    assert stored.completeness.comments.kind == "failed"
    assert (
        store.discovery_state(AuthorityId("peak-district")).queued[0].locator
        == f"{peak.LEGACY_BASE}/result/sanitised-0917"
    )
    view = store.application_view(report.applications[0])
    assert view.metadata.aliases == ("PP-15234567",)
    assert report.attachment_body_requests == 0
    store.close()


def test_camden_exact_resolution_and_public_package_collection() -> None:
    adapter = camden.CamdenAdapter()
    package = AuthorityPackage(
        adapter, camden.CamdenApplicationV1, camden.CamdenCheckpointV1
    )
    session = _Session(_CamdenMock())

    async def exercise() -> Any:
        reference = await adapter.resolve_exact(session, "2026/2706/L")
        return reference, await package.collect(session, reference)

    reference, collected = asyncio.run(exercise())
    assert reference.locator == "681726"
    assert collected.normalised.proposal == "Repair listed townhouse"
    assert [item.title for item in collected.normalised.documents] == [
        "Decision notice",
        "Site plan",
    ]
    assert collected.normalised.completeness.documents.kind == "complete"
    assert collected.normalised.completeness.comments.kind == "unavailable"
    assert collected.normalised.metadata.location is not None
    assert session.attachment_body_requests == 0
    assert all("/inline/" not in url for url in session.requested_urls)


def test_arun_resume_open_count_and_identity_boundaries() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 16), end=date(2026, 9, 16), include_open=False
    )

    async def first_batch() -> Any:
        batches = cast(
            "AsyncGenerator[Any]",
            adapter.discover(_Session(_ArunMock()), window, None),
        )
        first = await anext(batches)
        await batches.aclose()
        return first

    first = asyncio.run(first_batch())
    resumed_session = _Session(_ArunMock())
    resumed = asyncio.run(
        _batches(adapter, resumed_session, window, first.next_checkpoint)
    )
    assert [item.reference for batch in resumed for item in batch.references] == [
        "BR/157/25/PL"
    ]
    assert resumed[-1].complete
    assert (
        sum(
            request.method == RequestMethod.POST for request in resumed_session.requests
        )
        == 3
    )

    with pytest.raises(arun.ArunCountMismatchError):
        asyncio.run(
            _batches(adapter, _Session(_ArunMock(first_reported=0)), window, None)
        )
    with pytest.raises(arun.ArunCountMismatchError):
        asyncio.run(
            _batches(adapter, _Session(_ArunMock(first_show_all=False)), window, None)
        )
    with pytest.raises(arun.ArunCountMismatchError):
        asyncio.run(
            _batches(adapter, _Session(_ArunMock(expanded_reported=3)), window, None)
        )
    open_batches = asyncio.run(
        _batches(
            adapter,
            _Session(_ArunMock()),
            window.model_copy(update={"include_open": True}),
            None,
        )
    )
    assert open_batches[-1].complete
    open_cursor = open_batches[-1].next_checkpoint.cursor
    assert isinstance(open_cursor, arun.ArunLiveCursor)
    assert len(open_cursor.plan) == 60
    stale_scope = arun.ArunDiscoveryScope(
        start=date(2020, 1, 1),
        end=date(2020, 1, 2),
        include_open=False,
    )
    stale = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=stale_scope,
            plan=arun._canonical_query_plan(stale_scope),
            progress=arun.ArunReady(next_query=0),
        )
    )
    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(_batches(adapter, _Session(_ArunMock()), window, stale))
    reference = SourceReference(
        source_id=arun.SOURCE,
        reference="BR/156/25/PL",
        locator=f"{arun.BASE_URL}/planningDetails?reference=BR%2F156%2F25%2FPL",
    )
    with pytest.raises(arun.ArunReferenceMismatchError):
        asyncio.run(adapter.fetch(_Session(_ArunMock(mismatch_detail=True)), reference))
    with pytest.raises(arun.ArunRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_ArunMock()),
                reference.model_copy(update={"source_id": SourceId("other")}),
            )
        )


def test_devon_window_disclaimer_cap_and_identity_boundaries() -> None:
    adapter = devon.DevonAdapter(today=lambda: date(2026, 9, 16))
    window = DiscoveryWindow(
        start=date(2026, 6, 19), end=date(2026, 9, 16), include_open=False
    )
    with pytest.raises(devon.DevonWindowUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DevonMock()),
                window.model_copy(update={"start": date(2026, 6, 20)}),
                None,
            )
        )
    with pytest.raises(devon.DevonDisclaimerAcceptanceError):
        asyncio.run(
            _batches(
                adapter, _Session(_DevonMock(repeated_disclaimer=True)), window, None
            )
        )
    with pytest.raises(devon.DevonSearchCapUnsupportedError):
        asyncio.run(
            _batches(adapter, _Session(_DevonMock(pagination=True)), window, None)
        )
    with pytest.raises(devon.DevonCountMismatchError):
        asyncio.run(
            _batches(adapter, _Session(_DevonMock(direct=True, count=2)), window, None)
        )
    open_window = window.model_copy(update={"include_open": True})
    with pytest.raises(devon.DevonOpenEnumerationUnsupportedError):
        asyncio.run(
            _batches(adapter, _Session(_DevonMock(direct=True)), open_window, None)
        )
    stale = devon.DevonCheckpointV1(
        result_page="live", window_start=date(2020, 1, 1), window_end=date(2020, 1, 2)
    )
    with pytest.raises(devon.DevonCheckpointError):
        asyncio.run(_batches(adapter, _Session(_DevonMock()), window, stale))
    reference = SourceReference(
        source_id=devon.SOURCE,
        reference="DCC/4473/2026",
        locator=f"{devon.BASE_URL}/Planning/Display/DCC/4473/2026",
    )
    with pytest.raises(devon.DevonReferenceMismatchError):
        asyncio.run(
            adapter.fetch(
                _Session(_DevonMock(direct=True, mismatch_detail=True)), reference
            )
        )
    with pytest.raises(devon.DevonRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_DevonMock()), reference.model_copy(update={"locator": None})
            )
        )


def test_peak_window_count_open_and_identity_boundaries() -> None:
    adapter = peak.PeakDistrictAdapter(today=lambda: date(2026, 9, 16))
    window = DiscoveryWindow(
        start=date(2026, 9, 10), end=date(2026, 9, 16), include_open=False
    )
    with pytest.raises(peak.PeakDistrictWindowUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_PeakMock()),
                window.model_copy(update={"start": date(2026, 9, 11)}),
                None,
            )
        )
    with pytest.raises(peak.PeakDistrictCountMismatchError):
        asyncio.run(_batches(adapter, _Session(_PeakMock(reported=2)), window, None))
    with pytest.raises(peak.PeakDistrictResultWindowError):
        asyncio.run(
            _batches(adapter, _Session(_PeakMock(row_date="09/09/2026")), window, None)
        )
    with pytest.raises(peak.PeakDistrictOpenEnumerationUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_PeakMock()),
                window.model_copy(update={"include_open": True}),
                None,
            )
        )
    stale = peak.PeakDistrictCheckpointV1(
        row_offset="live", window_start=date(2020, 1, 1), window_end=date(2020, 1, 2)
    )
    with pytest.raises(peak.PeakDistrictCheckpointError):
        asyncio.run(_batches(adapter, _Session(_PeakMock()), window, stale))
    reference = SourceReference(
        source_id=peak.LEGACY_SOURCE,
        reference="NP/DIS/0926/0917",
        locator=f"{peak.LEGACY_BASE}/result/sanitised-0917",
    )
    with pytest.raises(peak.PeakDistrictReferenceMismatchError):
        asyncio.run(adapter.fetch(_Session(_PeakMock(mismatch_detail=True)), reference))
    with pytest.raises(peak.PeakDistrictRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_PeakMock()), reference.model_copy(update={"locator": None})
            )
        )
    complete = asyncio.run(adapter.fetch(_Session(_PeakMock(loading=False)), reference))
    assert complete.completeness.documents.kind == "unavailable"
    assert complete.completeness.comments.kind == "unavailable"


def test_camden_discovery_search_document_and_identity_boundaries() -> None:
    adapter = camden.CamdenAdapter()
    live = _Session(_CamdenMock())
    with pytest.raises(camden.CamdenBoundedDiscoveryUnavailableError):
        asyncio.run(
            _batches(
                adapter,
                live,
                DiscoveryWindow(start=date(2026, 9, 1), end=date(2026, 9, 16)),
                None,
            )
        )
    assert live.requests == []
    fixture = _Session(_CamdenMock(), mode=TransportMode.FIXTURE)
    with pytest.raises(camden.CamdenExactSearchLiveOnlyError):
        asyncio.run(adapter.resolve_exact(fixture, "2026/2706/L"))
    reference = SourceReference(
        source_id=camden.SEARCH_SOURCE, reference="2026/2706/L", locator="681726"
    )
    with pytest.raises(camden.CamdenReferenceMismatchError):
        asyncio.run(
            adapter.fetch(_Session(_CamdenMock(mismatch_detail=True)), reference)
        )
    with pytest.raises(camden.CamdenRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_CamdenMock()), reference.model_copy(update={"locator": None})
            )
        )
    failed = asyncio.run(
        adapter.fetch(_Session(_CamdenMock(document_failure=True)), reference)
    )
    assert failed.completeness.documents.kind == "failed"
    malformed = asyncio.run(
        adapter.fetch(_Session(_CamdenMock(document_reported=3)), reference)
    )
    assert malformed.completeness.documents.kind == "failed"


def test_arun_terminal_and_parser_boundaries() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 16), end=date(2026, 9, 16), include_open=False
    )
    scope = arun.ArunDiscoveryScope.model_validate(window.model_dump())
    plan = arun._canonical_query_plan(scope)
    completed = tuple(
        arun.ArunCompletedQuery(
            key=query.key,
            reported_count=0,
            enumerated_count=0,
        )
        for query in plan
    )
    terminal = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunComplete(completed=completed),
        )
    )
    batches = asyncio.run(_batches(adapter, _Session(_ArunMock()), window, terminal))
    assert batches[0].complete
    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_ArunMock()),
                window.model_copy(update={"include_open": True}),
                terminal,
            )
        )

    complete_mock = _ArunMock(first_reported=1, first_show_all=False)
    complete = asyncio.run(_batches(adapter, _Session(complete_mock), window, None))
    assert complete[-1].complete
    open_complete = asyncio.run(
        _batches(
            adapter,
            _Session(complete_mock),
            window.model_copy(update={"include_open": True}),
            None,
        )
    )
    assert open_complete[-1].complete
    show_all_checkpoint = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunAwaitingShowAll(
                next_query=0,
                reported_count=2,
                initial_references=("BR/156/25/PL",),
                seen_references=("BR/156/25/PL",),
            ),
        )
    )
    with pytest.raises(arun.ArunQueryReplayError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_ArunMock(first_show_all=False)),
                window,
                show_all_checkpoint,
            )
        )

    with pytest.raises(arun.ArunParseError, match="planning search form"):
        arun._parse_search_form(b"<html></html>")
    soup = BeautifulSoup(
        '<form><input name="skip" type="submit"><select name="empty"></select>'
        '<textarea name="notes"> note </textarea><input name="odd"></form>',
        "html.parser",
    )
    form = soup.form
    assert form is not None
    odd = form.select_one('input[name="odd"]')
    assert odd is not None
    odd["name"] = ["not-string"]  # type: ignore[assignment]
    assert [(field.name, field.value) for field in arun._form_fields(form)] == [
        ("empty", ""),
        ("notes", "note"),
    ]
    with pytest.raises(arun.ArunParseError, match="result reference"):
        arun._parse_search_results(
            b'<a href="planningDetails?from=planningSearch">View</a><p>1 result</p>'
        )
    duplicate = arun._parse_search_results(
        b'<a href="planningDetails?reference=A">A</a>'
        b'<a href="planningDetails?reference=A">A again</a><p>1 result</p>'
    )
    assert len(duplicate.references) == 1
    assert arun._parse_search_results(b"<p>No results</p>").reported == 0
    with pytest.raises(arun.ArunParseError, match="reported result count"):
        arun._parse_search_results(b"<p>Unknown</p>")
    fields = arun._parse_labelled_fields(
        b"<table><tr><td>orphan</td></tr></table>"
        b"<dl><dt>Proposal</dt><dd>Value</dd><dt>Orphan</dt></dl>"
    )
    assert fields == {"proposal": "Value"}
    with pytest.raises(arun.ArunParseError, match="labelled detail fields"):
        arun._parse_labelled_fields(b"<p>none</p>")
    assert arun._optional_field({"second": "value"}, "first", "second") == "value"
    assert arun._optional_field({}, "missing") is None
    with pytest.raises(arun.ArunParseError, match="detail missing"):
        arun._required_field({}, "missing")
    assert arun._optional_date({}, "date") is None
    with pytest.raises(arun.ArunParseError, match="date date"):
        arun._optional_date({"date": "bad"}, "date")


def test_devon_terminal_and_parser_boundaries() -> None:
    adapter = devon.DevonAdapter(today=lambda: date(2026, 9, 16))
    window = DiscoveryWindow(
        start=date(2026, 6, 19), end=date(2026, 9, 16), include_open=False
    )
    terminal = devon.DevonCheckpointV1(result_page="live", live_complete=True)
    assert asyncio.run(_batches(adapter, _Session(_DevonMock()), window, terminal))[
        0
    ].complete
    with pytest.raises(devon.DevonOpenEnumerationUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DevonMock()),
                window.model_copy(update={"include_open": True}),
                terminal,
            )
        )
    with pytest.raises(devon.DevonParseError, match="accepted disclaimer"):
        devon._parse_search_results(_devon_disclaimer("search"))
    with pytest.raises(devon.DevonParseError, match="detail link"):
        devon._parse_search_results(
            b'<p>Found 1 record</p><dl class="searchResultsList"></dl>'
        )
    fallback = devon._parse_search_results(
        b'<dl class="searchResultsList"><a href="/Planning/Display/DCC/1"></a></dl>'
    )
    assert fallback[0].reference == "DCC/1"
    assert devon._parse_search_results(b"<p>No records</p>") == ()
    with pytest.raises(devon.DevonParseError, match="no-records"):
        devon._parse_search_results(b"<p>Unknown</p>")
    with pytest.raises(devon.DevonParseError, match="details-grid"):
        devon._parse_labelled_fields(b'<dl class="details-grid"><dt>Orphan</dt></dl>')
    assert devon._optional_field({"second": "value"}, "first", "second") == "value"
    assert devon._optional_field({}, "missing") is None
    with pytest.raises(devon.DevonParseError, match="detail missing"):
        devon._required_field({}, "missing")
    assert devon._optional_date({}, "date") is None
    with pytest.raises(devon.DevonParseError, match="date date"):
        devon._optional_date({"date": "bad"}, "date")


def test_peak_terminal_and_parser_boundaries() -> None:
    adapter = peak.PeakDistrictAdapter(today=lambda: date(2026, 9, 16))
    window = DiscoveryWindow(
        start=date(2026, 9, 10), end=date(2026, 9, 16), include_open=False
    )
    terminal = peak.PeakDistrictCheckpointV1(row_offset="live", live_complete=True)
    assert asyncio.run(_batches(adapter, _Session(_PeakMock()), window, terminal))[
        0
    ].complete
    with pytest.raises(peak.PeakDistrictOpenEnumerationUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_PeakMock()),
                window.model_copy(update={"include_open": True}),
                terminal,
            )
        )
    seen = peak.PeakDistrictCheckpointV1(
        row_offset="live", seen_references=("NP/DIS/0926/0917",)
    )
    assert (
        asyncio.run(_batches(adapter, _Session(_PeakMock()), window, seen))[
            0
        ].references
        == ()
    )
    with pytest.raises(peak.PeakDistrictParseError, match="searchresults"):
        peak._parse_weekly_results(b"<html></html>", window)
    assert (
        peak._parse_weekly_results(
            b'<p data-result-count="0"></p><table id="searchresults"><tr><th>Head</th></tr></table>',
            window,
        )
        == ()
    )
    with pytest.raises(peak.PeakDistrictParseError, match="opaque result link"):
        peak._parse_weekly_results(
            b'<p data-result-count="1"></p><table id="searchresults"><tr><td>A</td><td>16/09/2026</td></tr></table>',
            window,
        )
    with pytest.raises(peak.PeakDistrictParseError, match="weekly reference"):
        peak._parse_weekly_results(
            b'<p data-result-count="1"></p><table id="searchresults"><tr><td></td><td>16/09/2026</td><td><a href="/result/x">View</a></td></tr></table>',
            window,
        )
    assert peak._reported_count(BeautifulSoup("No entries", "html.parser")) == 0
    assert (
        peak._reported_count(
            BeautifulSoup("Showing 1 to 10 of 12 entries", "html.parser")
        )
        == 12
    )
    with pytest.raises(peak.PeakDistrictParseError, match="reported result count"):
        peak._reported_count(BeautifulSoup("Unknown", "html.parser"))
    with pytest.raises(peak.PeakDistrictParseError, match="weekly row date"):
        peak._row_date([BeautifulSoup("<td>none</td>", "html.parser").td])  # type: ignore[list-item]
    fields, loading, assure = peak._parse_detail(
        b'<dl class="details"><dt>Proposal</dt><dd>Value</dd><dt>Orphan</dt></dl>'
    )
    assert fields == {"proposal": "Value"}
    assert loading == ()
    assert assure is None
    with pytest.raises(peak.PeakDistrictParseError, match="legacy labelled detail"):
        peak._parse_detail(b'<table class="details"><tr><td>orphan</td></tr></table>')
    assert peak._optional_field({"second": "value"}, "first", "second") == "value"
    assert peak._optional_field({}, "missing") is None
    with pytest.raises(peak.PeakDistrictParseError, match="detail missing"):
        peak._required_field({}, "missing")
    assert peak._optional_date({}, "date") is None
    with pytest.raises(peak.PeakDistrictParseError, match="date date"):
        peak._optional_date({"date": "bad"}, "date")


def test_camden_search_and_parser_boundaries() -> None:
    with pytest.raises(camden.CamdenParseError, match="JSF search form"):
        camden._parse_jsf_form(b"<html></html>")
    with pytest.raises(camden.CamdenParseError, match="ViewState"):
        camden._parse_jsf_form(b'<form id="searchForm"></form>')
    minimal_form = camden._parse_jsf_form(
        b'<form id="searchForm"><input name="javax.faces.ViewState" value="v"></form>'
    )
    request = camden._exact_search_request(minimal_form, "A/1")
    assert [field.name for field in request.form] == [
        "javax.faces.ViewState",
        "searchForm",
        "searchForm:searchTermInput:textField",
        "searchForm:SubmitButton:button",
    ]
    with pytest.raises(camden.CamdenExactSearchMismatchError):
        camden._parse_exact_result(b"<html></html>", "A/1")
    with pytest.raises(camden.CamdenParseError, match="numeric key"):
        camden._parse_exact_result(
            b'<a href="/NECSWS/Redirection/redirect.aspx?linkid=EXDC">A/1</a>',
            "A/1",
        )
    ignored = (
        b'<a href="/NECSWS/Redirection/redirect.aspx?PARAM0=1">Other</a>'
        b'<a href="/NECSWS/Redirection/redirect.aspx?PARAM0=2">A/1</a>'
    )
    assert camden._parse_exact_result(ignored, "A/1").locator == "2"
    with pytest.raises(camden.CamdenExactSearchMismatchError):
        camden._parse_exact_result(
            ignored + b'<a href="/NECSWS/Redirection/redirect.aspx?PARAM0=3">A/1</a>',
            "A/1",
        )

    assert (
        camden._parse_documents(
            b"<p>Reported 0 documents</p><table><tbody><tr><td>no link</td></tr></tbody></table>"
        )
        == ()
    )
    with pytest.raises(camden.CamdenParseError, match="document title"):
        camden._parse_documents(
            b'<p data-result-count="1"></p><div data-document-row><a href="/inline/x"></a></div>'
        )
    assert (
        camden._reported_document_count(BeautifulSoup("Total 2 records", "html.parser"))
        == 2
    )
    with pytest.raises(camden.CamdenParseError, match="document result count"):
        camden._reported_document_count(BeautifulSoup("Unknown", "html.parser"))
    orphan = BeautifulSoup("<tr><td>value</td></tr>", "html.parser").tr
    assert orphan is not None
    assert camden._row_values(orphan) == {}
    with pytest.raises(camden.CamdenParseError, match="Northgate dataview"):
        camden._parse_dataview(b"<html></html>")
    assert camden._parse_dataview(
        b'<div class="dataview"><table><tr><td>orphan</td></tr></table>'
        b"<dl><dt>Proposal</dt><dd>Value</dd><dt>Orphan</dt></dl></div>"
    ) == {"proposal": "Value"}
    with pytest.raises(camden.CamdenParseError, match="labelled values"):
        camden._parse_dataview(
            b'<div class="dataview"><table><tr><td>orphan</td></tr></table></div>'
        )
    assert camden._mapping_value({"second": "value"}, "first", "second") == "value"
    assert camden._mapping_value({}, "missing") is None
    with pytest.raises(camden.CamdenParseError, match="detail missing"):
        camden._required_field({}, "missing")
    with pytest.raises(camden.CamdenParseError, match="integer easting"):
        camden._required_integer({"easting": "bad"}, "easting")
    assert camden._parse_date(None) is None
    with pytest.raises(camden.CamdenParseError, match="document date"):
        camden._parse_date("bad")


async def _batches(
    adapter: Any, session: _Session, window: DiscoveryWindow, checkpoint: Any
) -> list[Any]:
    return [batch async for batch in adapter.discover(session, window, checkpoint)]
