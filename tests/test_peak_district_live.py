# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, E501, PLR2004, RUF012, SLF001

"""Peak District AssureLive request and checkpoint contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

import yimby.authorities.peak_district.adapter as peak
from yimby.domain import (
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceId,
    SourceReference,
    TransportMode,
)
from yimby.transport import PortalRequest, RequestMethod, SourceUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from types import ModuleType


class _Session:
    def __init__(self, responder: Callable[[PortalRequest], bytes]) -> None:
        self.responder = responder
        self.requests: list[PortalRequest] = []
        self._bytes = 0
        self.closed = False

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
        return TransportMode.LIVE

    async def aclose(self) -> None:
        self.closed = True


def _search_form() -> bytes:
    return b"""
    <form id="frmOnlinePlanningSearch">
      <input type="radio" name="SearchFor" value="PlanningApplications">
      <input type="radio" name="SearchFor" value="PlanningAppeals">
      <input type="hidden" id="IsAdvanceSearch" name="IsAdvanceSearch" value="false">
      <input type="hidden" id="IsPaginationClicked" name="IsPaginationClicked" value="false">
      <input type="hidden" id="urlOnlinePlanningSearchResult"
             value="/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResults">
      <input type="hidden" id="urlOnlinePlanningAdvanceSearchView"
             value="/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningAdvanceSearchView">
    </form>
    """


def _advanced_form(*, include_appeal: bool = True) -> bytes:
    appeal = (
        '<option value="APPEAL LODGED">APPEAL LODGED</option>' if include_appeal else ""
    )
    return f"""
    <div>
      <select name="AdvanceSearch.SelectedApplicationType"><option value="-1">Any application type</option></select>
      <select name="AdvanceSearch.SelectedDevelopmentType"><option value="-1">Any development type</option></select>
      <select name="AdvanceSearch.SelectedApplicationStatus">
        <option value="-1">Any status</option>
        {appeal}
        <option value="REGISTERED">REGISTERED</option>
        <option value="WITHDRAWN">WITHDRAWN</option>
      </select>
      <input type="radio" name="AdvanceSearch.ReceivedAnyTime" value="False" checked>
      <input type="radio" name="AdvanceSearch.ReceivedBetween" value="True">
      <input name="AdvanceSearch.ReceivedFromDate">
      <input name="AdvanceSearch.ReceivedToDate">
      <input type="radio" name="AdvanceSearch.ValidatedAnyTime" value="False" checked>
      <input type="radio" name="AdvanceSearch.ValidatedBetween" value="True">
      <input name="AdvanceSearch.ValidatedFromDate">
      <input name="AdvanceSearch.ValidatedToDate">
      <input type="radio" name="AdvanceSearch.DecidedAnyTime" value="False" checked>
      <input type="radio" name="AdvanceSearch.DecidedBetween" value="True">
      <input name="AdvanceSearch.DecidedFromDate">
      <input name="AdvanceSearch.DecidedToDate">
    </div>
    """.encode()


def _result_page(
    references: tuple[str, ...],
    *,
    reported: int,
    page: int = 0,
    page_size: int = 2,
    hidden_reported: int | None = None,
    visible_pages: tuple[int, ...] | None = None,
) -> bytes:
    rows = "".join(
        f"""
        <div class="row result">
          <a href="/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber={reference.replace("/", "%2F")}&amp;guid=session-{reference.replace("/", "-")}">View</a>
          <span>Application No: {reference} | Registered : 16 September 2026</span>
        </div>
        """
        for reference in references
    )
    pages = max(1, (reported + page_size - 1) // page_size)
    links = "".join(
        f'<a href="#" onclick="PagingClick(\'{index}\')">{index + 1}</a>'
        for index in (
            range(pages) if visible_pages is None else visible_pages
        )
    )
    hidden = reported if hidden_reported is None else hidden_reported
    return f"""
    <div>{rows}</div>
    <input name="PageCount" value="{pages}">
    <input name="PageSize" value="{page_size}">
    <input name="TotalRecords" value="{hidden}">
    <select name="PagingParameters.PageSize"><option selected>{page_size}</option></select>
    <input name="PagingParameters.CurrentPageIndex" value="{page}">
    <input name="PagingParameters.PageSize" value="{page_size}">
    <input name="PagingParameters.TotalRecords" value="0">
    <input name="IsPaginationClicked" value="true">
    <span>Total record(s): {reported}</span>
    <div class="pagination">{links}</div>
    <div id="generalSearchPagination" data-url="/AssureLive/ES/Presentation/Planning/OnlinePlanning/SearchResultsForPagination"></div>
    """.encode()


def _detail(
    reference: str = "NP/DDD/0926/0909",
    *,
    comments_tab: bool = False,
    documents_route: str = "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments",
) -> bytes:
    comments = (
        '<li id="Comments_tab"><a href="#tabComments">Comments</a></li>'
        if comments_tab
        else ""
    )
    return f"""
    <span id="spnApplicationId">{reference}</span>
    <input id="applicationReference" value="{reference}">
    <a id="applicationStatusHelpTextHeader">REGISTERED: Valid</a>
    <div class="row btspace tpspace"><div class="col-xs-12 padding-0">
      <div class="col-xs-12"><label>Listed Building Consent</label></div>
      <div class="col-xs-12"><label>Short proposal</label></div>
      <div class="col-xs-12"><label id="applicationDisplayAddress">1 Moor Road\nBakewell</label></div>
    </div></div>
    <ul id="myTab"><li id="Overview_tab"></li><li id="Documents_tab"></li>{comments}</ul>
    <div id="tabOverviewMain">
      <table class="boxBorderLightGrey">
        <tr><td><label>Proposal</label></td><td><label>Repair listed building</label></td></tr>
        <tr><td><label>Applicant</label></td><td><label>Applicant One</label></td></tr>
        <tr><td><label>Agent/Company</label></td><td><label>Agent One</label></td></tr>
        <tr><td><label>Planning officer</label></td><td><label>Officer One</label></td></tr>
        <tr><td><label>Registered</label></td><td><label>15 September 2026</label></td></tr>
        <tr><td><label>Decided</label></td><td><label></label></td></tr>
        <tr><td><label>Parish</label></td><td><label>Bakewell</label></td></tr>
      </table>
    </div>
    <div id="divDisplayDocumentsUrl" data-url="{documents_route}"></div>
    <div id="divDisplayCommentsUrl" data-url="/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetPlanningComments"></div>
    """.encode()


def _documents_page(
    documents: tuple[tuple[str, str, str, str], ...],
    *,
    reported: int,
    page: int = 0,
    page_size: int = 2,
) -> bytes:
    if reported == 0:
        return b'<div id="tabDocumentsMain"><form id="frmDocumentsMain"><strong>No record(s) found</strong></form></div>'
    rows = "".join(
        f"""
        <div class="row btspace">
          <div class="col-xs-2">{published}</div>
          <div class="col-xs-3"><a href="{url}"><u>{title}</u></a></div>
          <div class="col-xs-3"></div>
          <div class="col-xs-4">{document_type}</div>
        </div>
        """
        for title, url, published, document_type in documents
    )
    pages = (reported + page_size - 1) // page_size
    links = "".join(
        f"<a onclick=\"PagingClick('{index}')\">{index + 1}</a>"
        for index in range(pages)
    )
    return f"""
    <div id="tabDocumentsMain"><form id="frmDocumentsMain">
      <div id="divOnlinePlanningDocuments">
        <div class="row btspace"><strong>Received Date</strong></div>
        {rows}
      </div>
      <input name="DocumentCount" value="{reported}">
      <input name="PagingParameters.CurrentPageIndex" value="{page}">
      <input name="PagingParameters.PageSize" value="{page_size}">
      <input name="PagingParameters.TotalRecords" value="{reported}">
    </form>
    <span>Total record(s): {reported}</span>
    <ul class="pagination">{links}</ul>
    </div>
    """.encode()


class _PeakDetailMock:
    documents = (
        (
            "Application Form.pdf",
            "/AssureLive/ES/Presentation/Planning/OnlineDisplayDocument/DisplaySearchDocument/Application-Form.pdf?applicationNumber=NP%2FDDD%2F0926%2F0909&FileName=Application-Form.pdf&fileType=.pdf&aspectGuid=one",
            "14 September 2026",
            "Application Forms",
        ),
        (
            "Design Statement.pdf",
            "/AssureLive/ES/Presentation/Planning/OnlineDisplayDocument/DisplaySearchDocument/Design-Statement.pdf?applicationNumber=NP%2FDDD%2F0926%2F0909&FileName=Design-Statement.pdf&fileType=.pdf&aspectGuid=two",
            "14 September 2026",
            "Design and Access Statement",
        ),
        (
            "Site Plan.pdf",
            "/AssureLive/ES/Presentation/Planning/OnlineDisplayDocument/DisplaySearchDocument/Site-Plan.pdf?applicationNumber=NP%2FDDD%2F0926%2F0909&FileName=Site-Plan.pdf&fileType=.pdf&aspectGuid=three",
            "15 September 2026",
            "Plans and Drawings Planning Application",
        ),
    )

    def __init__(
        self,
        *,
        mismatch_detail: bool = False,
        empty_documents: bool = False,
        documents_fail: bool = False,
        documents_route: str = "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments",
    ) -> None:
        self.mismatch_detail = mismatch_detail
        self.empty_documents = empty_documents
        self.documents_fail = documents_fail
        self.documents_route = documents_route

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if "OnlinePlanningOverview" in url:
            return _detail(
                "WRONG/1" if self.mismatch_detail else "NP/DDD/0926/0909",
                documents_route=self.documents_route,
            )
        if "GetOnlineDocuments" in url:
            if self.documents_fail:
                raise SourceUnavailableError(url)
            query = parse_qs(urlsplit(url).query)
            assert query["applicationNumber"] == ["NP/DDD/0926/0909"]
            assert query["IsDatePublishSortedDescending"] == ["false"]
            assert query["pageSize"] == ["10"]
            page = int(query["currentPageIndex"][0])
            if self.empty_documents:
                return _documents_page((), reported=0)
            chunks = (self.documents[:2], self.documents[2:])
            return _documents_page(
                chunks[page],
                reported=3,
                page=page,
                page_size=2,
            )
        raise AssertionError(url)


class _QualificationResponder:
    def __init__(self, *, documents_fail: bool = False) -> None:
        self.discovery = _PeakAssureMock()
        self.documents_fail = documents_fail

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if "OnlinePlanningOverview" in url:
            reference = parse_qs(urlsplit(url).query)["applicationNumber"][0]
            return _detail(reference)
        if "GetOnlineDocuments" in url:
            if self.documents_fail:
                raise SourceUnavailableError(url)
            return _documents_page((), reported=0)
        return self.discovery(request)


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_peak_district.py"
    name = "_test_qualify_peak_district"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _PeakAssureMock:
    date_references = {
        "Received": (("NP/DDD/0926/0909", "NP/DIS/0926/0917"), ("NP/SM/0826/0810",)),
        "Validated": (("NP/DIS/0926/0917",),),
        "Decided": ((),),
    }
    status_references = {
        "REGISTERED": (("NP/DDD/0126/0001", "NP/DDD/0926/0909"),),
        "APPEAL LODGED": (("NP/DDD/1125/1200",),),
    }

    def __init__(
        self,
        *,
        include_appeal: bool = True,
        count_mismatch: bool = False,
    ) -> None:
        self.include_appeal = include_appeal
        self.count_mismatch = count_mismatch
        self.active_query: tuple[str, str] | None = None

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if request.method == RequestMethod.GET and url == peak._SEARCH_URL:
            return _search_form()
        if request.method == RequestMethod.GET and url == peak._ADVANCED_FORM_URL:
            return _advanced_form(include_appeal=self.include_appeal)
        fields = {field.name: field.value for field in request.form}
        if request.method != RequestMethod.POST:
            raise AssertionError(request)
        if url == peak._RESULTS_URL:
            assert fields["SearchFor"] == "PlanningApplications"
            assert fields["IsAdvanceSearch"] == "true"
            assert fields["PagingParameters.CurrentPageIndex"] == "0"
            query = self._query(fields)
            self.active_query = query
            return self._page(query, 0)
        if url == peak._PAGINATION_URL:
            assert fields["IsPaginationClicked"] == "true"
            assert self.active_query is not None
            assert self._query(fields) == self.active_query
            page = int(fields["PagingParameters.CurrentPageIndex"])
            return self._page(self.active_query, page)
        raise AssertionError(url)

    def _query(self, fields: dict[str, str]) -> tuple[str, str]:
        status = fields["AdvanceSearch.SelectedApplicationStatus"]
        if status != "-1":
            assert fields["AdvanceSearch.ReceivedAnyTime"] == "False"
            assert fields["AdvanceSearch.ValidatedAnyTime"] == "False"
            assert fields["AdvanceSearch.DecidedAnyTime"] == "False"
            return "status", status
        for date_field in ("Received", "Validated", "Decided"):
            if fields.get(f"AdvanceSearch.{date_field}Between") == "True":
                assert f"AdvanceSearch.{date_field}AnyTime" not in fields
                assert fields[f"AdvanceSearch.{date_field}FromDate"] == "18/08/2026"
                assert fields[f"AdvanceSearch.{date_field}ToDate"] == "16/09/2026"
                return "date", date_field
        raise AssertionError(fields)

    def _page(self, query: tuple[str, str], page: int) -> bytes:
        pages = (
            self.date_references[query[1]]
            if query[0] == "date"
            else self.status_references[query[1]]
        )
        references = pages[page]
        reported = sum(len(values) for values in pages)
        return _result_page(
            references,
            reported=reported,
            page=page,
            hidden_reported=(reported + 1 if self.count_mismatch else None),
        )


def _window(*, include_open: bool = True) -> DiscoveryWindow:
    return DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=include_open,
    )


async def _batches(
    adapter: peak.PeakDistrictAdapter,
    session: _Session,
    window: DiscoveryWindow,
    checkpoint: peak.PeakDistrictCheckpointV1 | None,
) -> list[Any]:
    return [batch async for batch in adapter.discover(session, window, checkpoint)]


def test_peak_district_query_inventory_is_exact() -> None:
    inventory = peak.peak_district_query_inventory(_window())

    assert [query.model_dump(mode="json") for query in inventory] == [
        {
            "kind": "bounded-date",
            "field": "Received",
            "value": "18/08/2026..16/09/2026",
        },
        {
            "kind": "bounded-date",
            "field": "Validated",
            "value": "18/08/2026..16/09/2026",
        },
        {
            "kind": "bounded-date",
            "field": "Decided",
            "value": "18/08/2026..16/09/2026",
        },
        {
            "kind": "older-open",
            "field": "AdvanceSearch.SelectedApplicationStatus",
            "value": "REGISTERED",
        },
        {
            "kind": "older-open",
            "field": "AdvanceSearch.SelectedApplicationStatus",
            "value": "APPEAL LODGED",
        },
    ]
    assert len({query.key for query in inventory}) == len(inventory)


def test_peak_district_discovers_bounded_and_older_open_with_exact_pagination() -> None:
    adapter = peak.PeakDistrictAdapter()
    session = _Session(_PeakAssureMock())

    batches = asyncio.run(_batches(adapter, session, _window(), None))

    assert [
        reference.reference for batch in batches for reference in batch.references
    ] == [
        "NP/DDD/0926/0909",
        "NP/DIS/0926/0917",
        "NP/SM/0826/0810",
        "NP/DDD/0126/0001",
        "NP/DDD/1125/1200",
    ]
    terminal = batches[-1].next_checkpoint
    assert terminal.live_complete
    assert terminal.active_query is None
    assert terminal.next_page_index == 0
    assert terminal.query_row_count == 0
    assert terminal.completed_queries == tuple(
        query.key for query in peak.peak_district_query_inventory(_window())
    )
    assert set(terminal.seen_references) == {
        "NP/DDD/0926/0909",
        "NP/DIS/0926/0917",
        "NP/SM/0826/0810",
        "NP/DDD/0126/0001",
        "NP/DDD/1125/1200",
    }
    assert all(
        reference.source_id == peak.LEGACY_SOURCE
        for batch in batches
        for reference in batch.references
    )
    assert all(
        "OnlinePlanningOverview" in cast("str", reference.locator)
        for batch in batches
        for reference in batch.references
    )
    assert (
        sum(
            request.method == RequestMethod.POST
            and str(request.url) == peak._RESULTS_URL
            for request in session.requests
        )
        == 5
    )
    assert (
        sum(
            request.method == RequestMethod.POST
            and str(request.url) == peak._PAGINATION_URL
            for request in session.requests
        )
        == 1
    )


def test_peak_district_resumes_active_page_and_terminal_rerun_is_zero_io() -> None:
    adapter = peak.PeakDistrictAdapter()
    first_session = _Session(_PeakAssureMock())

    async def first_batch() -> Any:
        stream = cast(
            "AsyncGenerator[Any]",
            adapter.discover(first_session, _window(), None),
        )
        first = await anext(stream)
        await stream.aclose()
        return first

    first = asyncio.run(first_batch())
    assert first.next_checkpoint.active_query is not None
    assert first.next_checkpoint.next_page_index == 1
    resumed_session = _Session(_PeakAssureMock())
    resumed = asyncio.run(
        _batches(adapter, resumed_session, _window(), first.next_checkpoint)
    )
    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == [
        "NP/SM/0826/0810",
        "NP/DDD/0126/0001",
        "NP/DDD/1125/1200",
    ]
    first_two_posts = [
        request
        for request in resumed_session.requests
        if request.method == RequestMethod.POST
    ][:2]
    assert [str(request.url) for request in first_two_posts] == [
        peak._RESULTS_URL,
        peak._PAGINATION_URL,
    ]
    terminal = resumed[-1].next_checkpoint
    terminal_session = _Session(lambda request: pytest.fail(str(request)))
    terminal_batches = asyncio.run(
        _batches(adapter, terminal_session, _window(), terminal)
    )
    assert terminal_batches[0].complete
    assert terminal_batches[0].references == ()
    assert terminal_session.requests == []


def test_peak_district_restarts_wrong_scope_and_omits_open_queries_when_disabled() -> (
    None
):
    adapter = peak.PeakDistrictAdapter()
    prior = peak.PeakDistrictCheckpointV1(
        row_offset="live",
        live_scope=peak.PeakDistrictDiscoveryScope(
            start=date(2026, 8, 17),
            end=date(2026, 9, 15),
            include_open=True,
        ),
        live_complete=True,
    )
    session = _Session(_PeakAssureMock())

    batches = asyncio.run(
        _batches(adapter, session, _window(include_open=False), prior)
    )

    assert batches[-1].next_checkpoint.live_complete
    assert len(batches[-1].next_checkpoint.completed_queries) == 3
    submitted_statuses = [
        {field.name: field.value for field in request.form}.get(
            "AdvanceSearch.SelectedApplicationStatus"
        )
        for request in session.requests
        if str(request.url) == peak._RESULTS_URL
    ]
    assert submitted_statuses == ["-1", "-1", "-1"]


def test_peak_district_fails_closed_on_form_and_count_drift() -> None:
    adapter = peak.PeakDistrictAdapter()
    with pytest.raises(peak.PeakDistrictParseError, match="open status options"):
        asyncio.run(
            _batches(
                adapter,
                _Session(_PeakAssureMock(include_appeal=False)),
                _window(),
                None,
            )
        )
    with pytest.raises(peak.PeakDistrictCountMismatchError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_PeakAssureMock(count_mismatch=True)),
                _window(),
                None,
            )
        )
    malformed = _result_page(
        ("NP/DDD/0926/0909",),
        reported=1,
        page=1,
    )
    with pytest.raises(peak.PeakDistrictParseError, match="result page index"):
        peak._parse_search_page(malformed, expected_page=0)
    bad_link = _result_page(("NP/DDD/0926/0909",), reported=1).replace(
        b"applicationNumber=NP%2FDDD%2F0926%2F0909",
        b"applicationNumber=WRONG%2F1",
    )
    with pytest.raises(peak.PeakDistrictReferenceMismatchError):
        peak._parse_search_page(bad_link, expected_page=0)
    assert parse_qs(
        urlsplit(
            str(
                peak._parse_search_page(
                    _result_page(("NP/DDD/0926/0909",), reported=1),
                    expected_page=0,
                )
                .references[0]
                .locator
            )
        ).query
    )["applicationNumber"] == ["NP/DDD/0926/0909"]


def _reference() -> SourceReference:
    return SourceReference(
        source_id=peak.LEGACY_SOURCE,
        reference="NP/DDD/0926/0909",
        locator=(
            f"{peak._ONLINE_BASE}/OnlinePlanningOverview"
            "?applicationNumber=NP%2FDDD%2F0926%2F0909&guid=session"
        ),
    )


def test_peak_district_fetches_all_document_metadata_without_bodies() -> None:
    adapter = peak.PeakDistrictAdapter()
    session = _Session(_PeakDetailMock())

    snapshot = asyncio.run(adapter.fetch(session, _reference()))
    normalised = adapter.normalise(snapshot)

    assert snapshot.payload.park_reference == "NP/DDD/0926/0909"
    assert snapshot.payload.record_type == "Listed Building Consent"
    assert snapshot.payload.proposal_summary == "Repair listed building"
    assert snapshot.payload.case_status == "REGISTERED: Valid"
    assert snapshot.payload.parish == "Bakewell"
    assert snapshot.payload.development_address == "1 Moor Road Bakewell"
    assert snapshot.payload.validated_date == date(2026, 9, 15)
    assert [document.title for document in snapshot.payload.documents] == [
        "Application Form.pdf",
        "Design Statement.pdf",
        "Site Plan.pdf",
    ]
    assert [document.document_type for document in snapshot.payload.documents] == [
        "Application Forms",
        "Design and Access Statement",
        "Plans and Drawings Planning Application",
    ]
    assert all(
        document.published_date is not None for document in snapshot.payload.documents
    )
    assert snapshot.completeness.documents.kind == "complete"
    assert snapshot.completeness.comments.kind == "unavailable"
    assert len(snapshot.evidence) == 3
    assert len(session.requests) == 3
    assert all(
        "OnlineDisplayDocument" not in str(request.url) for request in session.requests
    )
    assert [document.title for document in normalised.documents] == [
        "Application Form.pdf",
        "Design Statement.pdf",
        "Site Plan.pdf",
    ]
    assert normalised.metadata.officer_name == "Officer One"
    assert normalised.metadata.published_parties == (
        "Applicant One",
        "Agent One",
    )
    assert normalised.metadata.validated_date == date(2026, 9, 15)


def test_peak_district_classifies_empty_and_failed_document_sections() -> None:
    adapter = peak.PeakDistrictAdapter()
    empty = asyncio.run(
        adapter.fetch(_Session(_PeakDetailMock(empty_documents=True)), _reference())
    )
    assert empty.payload.documents == ()
    assert empty.completeness.documents.kind == "empty"

    failed = asyncio.run(
        adapter.fetch(_Session(_PeakDetailMock(documents_fail=True)), _reference())
    )
    assert failed.payload.documents == ()
    assert failed.completeness.documents.kind == "failed"

    drifted = asyncio.run(
        adapter.fetch(
            _Session(_PeakDetailMock(documents_route="/unexpected")),
            _reference(),
        )
    )
    assert drifted.completeness.documents.kind == "failed"


def test_peak_district_detail_identity_and_routing_fail_closed() -> None:
    adapter = peak.PeakDistrictAdapter()
    with pytest.raises(peak.PeakDistrictReferenceMismatchError):
        asyncio.run(
            adapter.fetch(_Session(_PeakDetailMock(mismatch_detail=True)), _reference())
        )
    with pytest.raises(peak.PeakDistrictRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_PeakDetailMock()),
                _reference().model_copy(update={"source_id": SourceId("other")}),
            )
        )
    with pytest.raises(peak.PeakDistrictRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_PeakDetailMock()),
                _reference().model_copy(update={"locator": None}),
            )
        )
    with pytest.raises(peak.PeakDistrictReferenceMismatchError):
        asyncio.run(
            adapter.fetch(
                _Session(_PeakDetailMock()),
                _reference().model_copy(update={"reference": "WRONG/1"}),
            )
        )


def test_peak_district_checkpoint_and_request_failure_boundaries() -> None:
    window = _window()
    queries = peak.peak_district_query_inventory(window)
    keys = tuple(query.key for query in queries)
    scope = peak.PeakDistrictDiscoveryScope(
        start=window.start,
        end=window.end,
        include_open=True,
    )
    invalid = (
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            completed_queries=(keys[0], keys[0]),
        ),
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            completed_queries=(keys[1],),
        ),
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            active_query="unknown",
        ),
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            completed_queries=(keys[0],),
            active_query=keys[0],
        ),
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            seen_references=("A", "A"),
        ),
        peak.PeakDistrictCheckpointV1(
            live_scope=scope,
            live_complete=True,
        ),
    )
    for checkpoint in invalid:
        with pytest.raises(peak.PeakDistrictCheckpointError):
            peak._assert_checkpoint(checkpoint, keys)

    partial = peak.PeakDistrictCheckpointV1(
        row_offset="live",
        window_start=window.start,
        window_end=window.end,
        live_scope=scope,
        completed_queries=(keys[0],),
        seen_references=(
            "NP/DDD/0926/0909",
            "NP/DIS/0926/0917",
            "NP/SM/0826/0810",
        ),
    )
    session = _Session(_PeakAssureMock())
    batches = asyncio.run(
        _batches(peak.PeakDistrictAdapter(), session, window, partial)
    )
    assert batches[-1].next_checkpoint.live_complete
    assert (
        sum(str(request.url) == peak._RESULTS_URL for request in session.requests) == 4
    )

    form = peak._parse_search_form(_search_form(), _advanced_form())
    bad_bounded = queries[0].model_copy(update={"value": "invalid"})
    with pytest.raises(peak.PeakDistrictParseError, match="bounded query"):
        peak._query_request(form, bad_bounded)
    bad_open = queries[-1].model_copy(update={"value": "COMPLETE"})
    with pytest.raises(peak.PeakDistrictParseError, match="open query"):
        peak._query_request(form, bad_open)
    with pytest.raises(peak.PeakDistrictParseError, match="pagination form"):
        peak._pagination_request(form, None, 1)
    with pytest.raises(peak.PeakDistrictCountMismatchError):
        peak._advance_checkpoint(
            peak.PeakDistrictCheckpointV1(),
            active_page=peak._ActivePage(
                query_key=keys[0],
                page_index=0,
                row_count=1,
            ),
            search_page=peak._SearchPage(
                references=(),
                reported=2,
                page_index=0,
                page_size=1,
                form=(),
            ),
            all_query_keys=keys,
        )


def test_peak_district_form_and_search_parser_failure_boundaries() -> None:
    with pytest.raises(peak.PeakDistrictParseError, match="search form"):
        peak._parse_search_form(b"<html></html>", _advanced_form())
    with pytest.raises(peak.PeakDistrictParseError, match="search route"):
        peak._parse_search_form(
            _search_form().replace(b"OnlinePlanningSearchResults", b"Changed"),
            _advanced_form(),
        )
    with pytest.raises(peak.PeakDistrictParseError, match="open status options"):
        peak._parse_search_form(_search_form(), b"<html></html>")
    with pytest.raises(peak.PeakDistrictParseError, match="open status options"):
        peak._parse_search_form(
            _search_form(),
            _advanced_form().replace(
                b'name="AdvanceSearch.SelectedApplicationStatus"',
                b'name="changed"',
            ),
        )
    with pytest.raises(peak.PeakDistrictParseError, match="advanced date field"):
        peak._parse_search_form(
            _search_form(),
            _advanced_form().replace(
                b'name="AdvanceSearch.ReceivedToDate"',
                b'name="changed"',
            ),
        )

    controls = BeautifulSoup(
        """
        <form>
          <input name="disabled" value="x" disabled>
          <input name="submit" type="submit" value="x">
          <input name="unchecked" type="checkbox" value="x">
          <input name="checked" type="checkbox" value="yes" checked>
          <select name="empty"></select>
          <textarea name="notes"> value </textarea>
        </form>
        """,
        "html.parser",
    ).form
    assert controls is not None
    assert [
        (field.name, field.value) for field in peak._successful_controls(controls)
    ] == [
        ("checked", "yes"),
        ("empty", ""),
        ("notes", "value"),
    ]

    valid = _result_page(("NP/DDD/0926/0909",), reported=1)
    decided = valid.replace(b"| Registered :", b"| Decided:")
    assert (
        peak._parse_search_page(decided, expected_page=0).references[0].reference
        == "NP/DDD/0926/0909"
    )
    large = _result_page(
        ("NP/DDD/0926/0909", "NP/SM/0926/0913"),
        reported=25,
        page_size=2,
        visible_pages=tuple(range(10)),
    )
    assert peak._parse_search_page(large, expected_page=0).reported == 25
    failures = (
        (
            valid.replace(b"SearchResultsForPagination", b"Changed"),
            "search results",
        ),
        (valid.replace(b"Total record(s): 1", b"Unknown"), "reported result count"),
        (
            valid.replace(
                b'name="PageSize" value="2"',
                b'name="PageSize" value="0"',
            ),
            "result page size",
        ),
        (
            valid.replace(b'name="PageCount" value="1"', b'name="PageCount" value="2"'),
            "reported 1 results",
        ),
        (_result_page(("NP/DDD/0926/0909",), reported=2), "reported 2 results"),
        (valid.replace(b"PagingClick('0')", b"NoPaging()"), "pagination inventory"),
    )
    for body, message in failures:
        with pytest.raises(
            (peak.PeakDistrictParseError, peak.PeakDistrictCountMismatchError),
            match=message,
        ):
            peak._parse_search_page(body, expected_page=0)
    with pytest.raises(peak.PeakDistrictParseError, match="result control"):
        peak._required_control_int(BeautifulSoup("<p></p>", "html.parser"), "missing")
    with pytest.raises(peak.PeakDistrictParseError, match="result control"):
        peak._required_control_int(
            BeautifulSoup('<input name="value" value="bad">', "html.parser"),
            "value",
        )

    no_application = valid.replace(
        b"applicationNumber=NP%2FDDD%2F0926%2F0909",
        b"other=value",
    )
    with pytest.raises(peak.PeakDistrictParseError, match="application number"):
        peak._parse_search_page(no_application, expected_page=0)
    no_row = valid.replace(b'<div class="row result">', b"<section>").replace(
        b"</div>\n        ", b"</section>\n        ", 1
    )
    with pytest.raises(peak.PeakDistrictParseError, match="result row"):
        peak._parse_search_page(no_row, expected_page=0)
    no_reference = valid.replace(b"Application No:", b"Reference:")
    with pytest.raises(peak.PeakDistrictParseError, match="result reference"):
        peak._parse_search_page(no_reference, expected_page=0)
    duplicate = valid.replace(
        b"</span>\n        </div>",
        b'</span><a href="/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=NP%2FDDD%2F0926%2F0909&amp;guid=other">View</a></div>',
        1,
    )
    with pytest.raises(
        peak.PeakDistrictParseError, match="conflicting result locators"
    ):
        peak._parse_search_page(duplicate, expected_page=0)


def test_peak_district_detail_and_document_parser_failure_boundaries() -> None:  # noqa: PLR0915
    valid_detail = _detail()
    detail_failures = (
        (
            valid_detail.replace(b'id="applicationReference"', b'id="changed"'),
            "application reference",
        ),
        (valid_detail.replace(b"Listed Building Consent", b"", 1), "record type"),
        (
            valid_detail.replace(b'id="spnApplicationId"', b'id="changed"'),
            "detail reference",
        ),
        (valid_detail.replace(b"REGISTERED: Valid", b""), "detail status"),
    )
    for body, message in detail_failures:
        with pytest.raises(peak.PeakDistrictParseError, match=message):
            peak._parse_assure_detail(body)
    without_optional = valid_detail.replace(
        b'<div class="col-xs-12"><label id="applicationDisplayAddress">1 Moor Road\nBakewell</label></div>',
        b"",
    ).replace(
        b'<div id="divDisplayDocumentsUrl" data-url="/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments"></div>',
        b"",
    )
    parsed = peak._parse_assure_detail(without_optional)
    assert parsed.address is None
    assert parsed.documents_endpoint is None
    with_short_row = valid_detail.replace(
        b'<table class="boxBorderLightGrey">',
        b'<table class="boxBorderLightGrey"><tr><td>orphan</td></tr>',
        1,
    )
    assert peak._parse_assure_detail(with_short_row).reference == "NP/DDD/0926/0909"

    def comments_responder(request: PortalRequest) -> bytes:
        if "OnlinePlanningOverview" in str(request.url):
            return _detail(comments_tab=True)
        return _documents_page((), reported=0)

    comments = asyncio.run(
        peak.PeakDistrictAdapter().fetch(_Session(comments_responder), _reference())
    )
    assert comments.completeness.comments.kind == "failed"
    invalid_locator = _reference().model_copy(
        update={"locator": "https://example.com/"}
    )
    with pytest.raises(peak.PeakDistrictRoutingError):
        asyncio.run(
            peak.PeakDistrictAdapter().fetch(
                _Session(_PeakDetailMock()),
                invalid_locator,
            )
        )

    with pytest.raises(peak.PeakDistrictParseError, match="documents container"):
        peak._parse_document_page(b"<html></html>", reference="A/1", expected_page=0)
    empty = _documents_page((), reported=0)
    with pytest.raises(peak.PeakDistrictParseError, match="empty state"):
        peak._parse_document_page(empty, reference="A/1", expected_page=1)
    document = _PeakDetailMock.documents[:1]
    page = _documents_page(document, reported=1)
    document_failures = (
        (page.replace(b"Total record(s): 1", b"Unknown"), "reported count"),
        (
            page.replace(
                b'name="DocumentCount" value="1"', b'name="DocumentCount" value="2"'
            ),
            "reported 1 results",
        ),
        (
            page.replace(
                b'name="PagingParameters.CurrentPageIndex" value="0"',
                b'name="PagingParameters.CurrentPageIndex" value="1"',
            ),
            "page index",
        ),
        (
            page.replace(
                b'name="PagingParameters.PageSize" value="2"',
                b'name="PagingParameters.PageSize" value="0"',
            ),
            "page size",
        ),
        (_documents_page((), reported=1), "reported 1 results"),
        (page.replace(b"PagingClick('0')", b"NoPaging()"), "pagination inventory"),
    )
    for body, message in document_failures:
        with pytest.raises(
            (peak.PeakDistrictParseError, peak.PeakDistrictCountMismatchError),
            match=message,
        ):
            peak._parse_document_page(
                body,
                reference="NP/DDD/0926/0909",
                expected_page=0,
            )

    valid_link = BeautifulSoup(page, "html.parser").select_one("a[href]")
    assert valid_link is not None
    bad_link = BeautifulSoup(
        str(valid_link).replace("aspectGuid=one", "other=one"), "html.parser"
    ).a
    assert bad_link is not None
    with pytest.raises(peak.PeakDistrictParseError, match="metadata link"):
        peak._parse_document_link(bad_link, "NP/DDD/0926/0909")
    orphan = BeautifulSoup(str(valid_link), "html.parser").a
    assert orphan is not None
    with pytest.raises(peak.PeakDistrictParseError, match="metadata row"):
        peak._parse_document_link(orphan, "NP/DDD/0926/0909")
    short_row = BeautifulSoup(f'<div class="row">{valid_link}</div>', "html.parser").a
    assert short_row is not None
    with pytest.raises(peak.PeakDistrictParseError, match="metadata row"):
        peak._parse_document_link(short_row, "NP/DDD/0926/0909")
    bad_metadata = BeautifulSoup(
        str(page).replace("14 September 2026", "bad date"), "html.parser"
    ).select_one("a[href]")
    assert bad_metadata is not None
    with pytest.raises(peak.PeakDistrictParseError, match="document metadata"):
        peak._parse_document_link(bad_metadata, "NP/DDD/0926/0909")

    malformed_documents, malformed_state = asyncio.run(
        peak._fetch_assure_documents(
            _Session(lambda _request: b"<html></html>"),
            "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments",
            "NP/DDD/0926/0909",
            [],
        )
    )
    assert malformed_documents == ()
    assert malformed_state.kind == "failed"

    count_mismatch_page = page.replace(
        b'name="DocumentCount" value="1"',
        b'name="DocumentCount" value="2"',
    )
    mismatched_documents, mismatched_state = asyncio.run(
        peak._fetch_assure_documents(
            _Session(lambda _request: count_mismatch_page),
            "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments",
            "NP/DDD/0926/0909",
            [],
        )
    )
    assert mismatched_documents == ()
    assert mismatched_state.kind == "failed"

    duplicate_pages = (
        _documents_page(document, reported=2, page=0, page_size=1),
        _documents_page(document, reported=2, page=1, page_size=1),
    )

    def duplicate_responder(request: PortalRequest) -> bytes:
        page_index = int(
            parse_qs(urlsplit(str(request.url)).query)["currentPageIndex"][0]
        )
        return duplicate_pages[page_index]

    duplicate_documents, duplicate_state = asyncio.run(
        peak._fetch_assure_documents(
            _Session(duplicate_responder),
            "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments",
            "NP/DDD/0926/0909",
            [],
        )
    )
    assert duplicate_documents == ()
    assert duplicate_state.kind == "failed"

    assert peak._optional_field({"second": "value"}, "first", "second") == "value"
    assert peak._optional_field({}, "missing") is None
    with pytest.raises(peak.PeakDistrictParseError, match="detail missing"):
        peak._required_field({}, "missing")
    assert peak._optional_date({}, "date") is None
    with pytest.raises(peak.PeakDistrictParseError, match="date date"):
        peak._optional_date({"date": "bad"}, "date")
    assert peak._parse_date("unknown") is None


def test_peak_district_qualification_requires_safe_exact_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    created = 0

    def session_factory() -> _Session:
        nonlocal created
        created += 1
        return _Session(_QualificationResponder())

    base = [
        "--data-dir",
        str(tmp_path / "missing-confirmation"),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(base, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "confirmation-required"

    without_open = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "missing-open"),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
    ]
    assert module.main(without_open, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "include-open-required"

    wrong_days = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "wrong-days"),
        "--start",
        "2026-08-19",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(wrong_days, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "thirty-days-required"

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("value", encoding="utf-8")
    occupied_args = [
        "--confirm-live",
        "--data-dir",
        str(occupied),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(occupied_args, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert (occupied / "preserve").read_text(encoding="utf-8") == "value"
    assert created == 0


def test_peak_district_qualification_persists_complete_receipt_and_zero_io_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification-peak-district-2026-09-16"
    sessions: list[_Session] = []

    def session_factory() -> _Session:
        session = _Session(_QualificationResponder())
        sessions.append(session)
        return session

    args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    result = module.main(
        args,
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 0
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == 18
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "peak-district"
    assert receipt["created_at"] == "2026-09-16T12:00:00Z"
    assert receipt["scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert [query["value"] for query in receipt["query_inventory"]] == [
        "18/08/2026..16/09/2026",
        "18/08/2026..16/09/2026",
        "18/08/2026..16/09/2026",
        "REGISTERED",
        "APPEAL LODGED",
    ]
    assert receipt["counts"] == {
        "applications": 5,
        "discovered_references": 5,
        "native_versions": 5,
        "application_versions": 5,
        "document_versions": 5,
        "comment_versions": 0,
        "pending_retries": 0,
        "failed_sections": 0,
        "unmapped_records": 0,
    }
    assert receipt["costs"]["initial"]["request_count"] == 18
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert receipt["weekly_cycles"] == [
        {"cycle": 1, "eligible_on": "2026-09-23", "status": "pending"},
        {"cycle": 2, "eligible_on": "2026-09-30", "status": "pending"},
    ]
    assert {check["name"]: check["ok"] for check in receipt["checks"]} == {
        "application-count": True,
        "attachment-policy": True,
        "database-integrity": True,
        "evidence-integrity": True,
        "evidence-paths": True,
        "exact-query-inventory": True,
        "failed-sections": True,
        "idempotent-rerun": True,
        "pending-retries": True,
        "reference-application-agreement": True,
        "run-statuses": True,
        "terminal-checkpoint": True,
        "terminal-rerun-io": True,
        "unmapped-records": True,
    }
    receipt_path = data_dir / "peak-district-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert not (data_dir / ".peak-district-qualification-v1.json.tmp").exists()

    sessions.clear()
    assert (
        module.main(
            [*args, "--resume"],
            session_factory=session_factory,
            now=lambda: datetime(2026, 9, 16, 13, tzinfo=UTC),
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)
    assert resumed["costs"]["initial"]["request_count"] == 0
    assert resumed["costs"]["rerun"]["request_count"] == 0


def test_peak_district_qualification_rejects_failed_current_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "failed-sections"
    sessions: list[_Session] = []

    def session_factory() -> _Session:
        session = _Session(_QualificationResponder(documents_fail=True))
        sessions.append(session)
        return session

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-08-18",
            "--end",
            "2026-09-16",
            "--include-open",
        ],
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert len(sessions) == 1
    assert sessions[0].closed
    assert not (data_dir / "peak-district-qualification-v1.json").exists()
