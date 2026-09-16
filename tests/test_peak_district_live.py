# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, E501, PLR2004, RUF012, SLF001

"""Peak District AssureLive request and checkpoint contracts."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

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


class _Session:
    def __init__(self, responder: Callable[[PortalRequest], bytes]) -> None:
        self.responder = responder
        self.requests: list[PortalRequest] = []
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
        return TransportMode.LIVE

    async def aclose(self) -> None:
        return None


def _search_form() -> bytes:
    return b"""
    <form id="frmOnlinePlanningSearch">
      <input type="radio" name="SearchFor" value="PlanningApplications" checked>
      <input type="radio" name="SearchFor" value="PlanningAppeals">
      <input type="hidden" id="IsAdvanceSearch" name="IsAdvanceSearch" value="false">
      <input type="hidden" id="IsPaginationClicked" name="IsPaginationClicked" value="false">
      <input type="hidden" id="urlOnlinePlanningSearchResult"
             value="/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResults">
      <input type="hidden" id="urlOnlinePlanningAdvanceSearchView"
             value="/AssureLive/ES/Presentation/Planning/OnlinePlanning/AdvanceSearch">
    </form>
    """


def _advanced_form(*, include_appeal: bool = True) -> bytes:
    appeal = (
        '<option value="APPEAL LODGED">APPEAL LODGED</option>' if include_appeal else ""
    )
    return f"""
    <fieldset id="fldOnlinePlanningSearchAdvanceSearch">
      <select name="AdvanceSearch.SelectedApplicationType"><option value="-1">Any application type</option></select>
      <select name="AdvanceSearch.SelectedDevelopmentType"><option value="-1">Any development type</option></select>
      <select name="AdvanceSearch.SelectedApplicationStatus">
        <option value="-1">Any status</option>
        {appeal}
        <option value="REGISTERED">REGISTERED</option>
        <option value="WITHDRAWN">WITHDRAWN</option>
      </select>
      <input type="radio" name="Received" value="False" checked>
      <input type="radio" name="Received" value="True">
      <input name="AdvanceSearch.ReceivedFromDate" disabled>
      <input name="AdvanceSearch.ReceivedToDate" disabled>
      <input type="radio" name="Validated" value="False" checked>
      <input type="radio" name="Validated" value="True">
      <input name="AdvanceSearch.ValidatedFromDate" disabled>
      <input name="AdvanceSearch.ValidatedToDate" disabled>
      <input type="radio" name="Decided" value="False" checked>
      <input type="radio" name="Decided" value="True">
      <input name="AdvanceSearch.DecidedFromDate" disabled>
      <input name="AdvanceSearch.DecidedToDate" disabled>
    </fieldset>
    """.encode()


def _result_page(
    references: tuple[str, ...],
    *,
    reported: int,
    page: int = 0,
    page_size: int = 2,
    hidden_reported: int | None = None,
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
        for index in range(pages)
    )
    hidden = reported if hidden_reported is None else hidden_reported
    return f"""
    <div id="divOnlinePlanningSearchResults">{rows}</div>
    <input name="PagingParameters.CurrentPageIndex" value="{page}">
    <input name="PagingParameters.PageSize" value="{page_size}">
    <input name="PagingParameters.TotalRecords" value="{hidden}">
    <input name="IsPaginationClicked" value="true">
    <span>Total record(s): {reported}</span>
    <div id="generalSearchPagination" data-url="/AssureLive/ES/Presentation/Planning/OnlinePlanning/SearchResultsForPagination">{links}</div>
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
            page = int(fields["PagingParameters.CurrentPageIndex"])
            return self._page(self.active_query, page)
        raise AssertionError(url)

    def _query(self, fields: dict[str, str]) -> tuple[str, str]:
        status = fields["AdvanceSearch.SelectedApplicationStatus"]
        if status != "-1":
            assert fields["Received"] == "False"
            assert fields["Validated"] == "False"
            assert fields["Decided"] == "False"
            return "status", status
        for date_field in ("Received", "Validated", "Decided"):
            if fields[date_field] == "True":
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
        start=peak.date(2026, 8, 18),
        end=peak.date(2026, 9, 16),
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
            start=peak.date(2026, 8, 17),
            end=peak.date(2026, 9, 15),
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
    assert snapshot.payload.validated_date == peak.date(2026, 9, 15)
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
    assert normalised.metadata.validated_date == peak.date(2026, 9, 15)


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
