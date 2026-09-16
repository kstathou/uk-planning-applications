# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: SLF001

"""Leeds live qualification behavior."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl

import httpx
import pytest
from bs4 import BeautifulSoup

import yimby.authorities.leeds.adapter as leeds_adapter
from yimby.authorities.leeds.adapter import (
    SOURCE,
    LeedsAdapter,
    LeedsApplicationV1,
    LeedsCheckpointV1,
    LeedsDiscoveryScope,
    LeedsParseError,
)
from yimby.domain import (
    ApplicationId,
    AuthorityId,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    EmptySection,
    FailedSection,
    NativeSnapshot,
    RetainedNativeRecord,
    SourceReference,
    UnavailableSection,
)
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.transport import FormField, SourceUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from types import ModuleType

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)

CASE_TYPES = (
    ("DAG", "Agricultural Determination"),
    ("ADV", "Application to Display Adverts"),
    ("CLA", "Certificate Alternative Appropriate Dev"),
    ("CLE", "Certificate of Existing Lawful Use"),
    ("CLP", "Certificate of Proposed Lawful Use"),
    ("DEM", "Demolition Notification"),
    ("COND", "Discharge of Conditions"),
    ("EXT", "Extension of Time Period"),
    ("EDD", "Extension to Determination Date"),
    ("BNG106", "Floating S106/BNG"),
    ("FU", "Full Planning Application"),
    ("HAZ", "Hazardous Substance Consent"),
    ("DHH", "Householder Determination"),
    ("LI", "Listed Building Application"),
    ("LA", "Local Authority Application Reg 4(1)"),
    ("LATR", "Local Authority Tree Works"),
    ("S106", "Modify or Discharge S106 Agreement"),
    ("MOD", "Non Material Amendment"),
    ("N1490", "Notification of Overhead Line"),
    ("NPD", "Notification under Permitted Development"),
    ("OT", "Outline Planning Application"),
    ("DPD", "Permitted Development Determination"),
    ("PIP", "Planning Permission in Principle"),
    ("PRESME", "Pre-Application (SME Builders)"),
    ("RM", "Reserved Matters Application"),
    ("TDC", "Technical Details Consent"),
    ("DTM", "Telecommunications Determination"),
    ("TWA", "Transport and Works Act 1992"),
    ("TR", "Tree Works"),
    ("UNK", "Unknown"),
)
EXPECTED_QUERY_COUNT = 43
EXPECTED_ADVANCED_QUERY_COUNT = 33
EXPECTED_QUALIFICATION_PASSES = 2
CONFIG_ERROR_EXIT = 2
SECOND_PAGE = 2


def _weekly_form() -> bytes:
    options = "".join(
        f'<option value="{value}">{value}</option>'
        for value in (
            "17/08/2026",
            "24/08/2026",
            "31/08/2026",
            "07/09/2026",
            "14/09/2026",
        )
    )
    return f"""
    <form>
      <input type="hidden" name="_csrf" value="weekly-token">
      <select name="week">{options}</select>
      <select name="dateType"><option value="DC_Validated">Validated</option></select>
      <input type="hidden" name="searchType" value="Application">
    </form>
    """.encode()


def _live_weekly_form() -> bytes:
    """Render the week values exactly as the live Leeds portal does."""
    return (
        _weekly_form()
        .replace(b"/08/2026", b" Aug 2026")
        .replace(
            b"/09/2026",
            b" Sep 2026",
        )
    )


def _options(values: tuple[tuple[str, str], ...]) -> str:
    return '<option value="">All</option>' + "".join(
        f'<option value="{value}">{label}</option>' for value, label in values
    )


def _advanced_form(*, case_types: tuple[tuple[str, str], ...] = CASE_TYPES) -> bytes:
    fields = (
        "searchCriteria.reference",
        "searchCriteria.description",
        "searchCriteria.applicantName",
        "searchCriteria.ward",
        "searchCriteria.parish",
        "searchCriteria.conservationArea",
        "searchCriteria.agent",
        "searchCriteria.caseDecision",
        "searchCriteria.developmentType",
        "searchCriteria.address",
        "date(applicationValidatedStart)",
        "date(applicationValidatedEnd)",
        "date(applicationCommitteeStart)",
        "date(applicationCommitteeEnd)",
        "date(applicationDecisionStart)",
        "date(applicationDecisionEnd)",
    )
    inputs = "".join(f'<input name="{name}" value="">' for name in fields)
    case_statuses = _options(
        (("Current", "Current"), ("Decided", "Decided"), ("Unknown", "Unknown"))
    )
    appeal_statuses = _options(
        (
            ("Appeal decided", "Appeal decided"),
            ("Appeal lodged", "Appeal lodged"),
            ("Unknown", "Unknown"),
        )
    )
    return f"""
    <form id="advancedSearchForm" method="post"
          action="advancedSearchResults.do?action=firstPage">
      <input type="hidden" name="_csrf" value="advanced-token">
      {inputs}
      <select name="searchCriteria.caseType">{_options(case_types)}</select>
      <select name="searchCriteria.caseStatus">{case_statuses}</select>
      <select name="searchCriteria.appealStatus">{appeal_statuses}</select>
      <input type="hidden" name="caseAddressType" value="Application">
      <input type="hidden" name="searchType" value="Application">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
    </form>
    """.encode()


class _LeedsSearchMock:
    def __init__(
        self,
        *,
        case_types: tuple[tuple[str, str], ...] = CASE_TYPES,
        capped_case_type: str | None = None,
        reject_requests: bool = False,
    ) -> None:
        self.case_types = case_types
        self.capped_case_type = capped_case_type
        self.reject_requests = reject_requests
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.reject_requests:
            message = f"unexpected request {request.method} {request.url}"
            raise AssertionError(message)
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        path = request.url.path
        action = request.url.params.get("action")
        if path.endswith("/search.do") and action == "weeklyList":
            return httpx.Response(200, content=_weekly_form())
        if path.endswith("/weeklyListResults.do"):
            return httpx.Response(200, content=b"<p>No results found</p>")
        if path.endswith("/search.do") and action == "advanced":
            return httpx.Response(
                200,
                content=_advanced_form(case_types=self.case_types),
            )
        if path.endswith("/advancedSearchResults.do"):
            values = dict(fields)
            if values.get("searchCriteria.caseType") == self.capped_case_type:
                return httpx.Response(
                    200,
                    content=(
                        b"<p>Please check the search criteria: Too many results found. "
                        b"Please enter some more parameters.</p>"
                    ),
                )
            return httpx.Response(200, content=b"<p>No results found</p>")
        message = f"unexpected request {request.method} {request.url}"
        raise AssertionError(message)


def _session(
    mock: Callable[[httpx.Request], httpx.Response],
) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )


async def _discover(
    mock: _LeedsSearchMock,
    checkpoint: LeedsCheckpointV1 | None = None,
) -> list[DiscoveryBatch[LeedsCheckpointV1]]:
    session = _session(mock)
    try:
        return [
            batch
            async for batch in LeedsAdapter().discover(
                session,
                WINDOW,
                checkpoint,
            )
        ]
    finally:
        await session.aclose()


def _completion_key(completion: object) -> str:
    return str(getattr(completion, "key", completion))


def test_leeds_runs_weekly_and_complete_advanced_inventory() -> None:
    """The bootstrap reconciles weekly, bounded, current, and appeal searches."""
    mock = _LeedsSearchMock()

    batches = asyncio.run(_discover(mock))

    checkpoint = batches[-1].next_checkpoint
    completed = tuple(_completion_key(item) for item in checkpoint.completed_queries)
    assert len(completed) == EXPECTED_QUERY_COUNT
    assert completed[:2] == (
        "17/08/2026|DC_Validated",
        "17/08/2026|DC_Decided",
    )
    assert completed[10:12] == (
        "advanced|validated|2026-08-18|2026-09-16",
        "advanced|decision|2026-08-18|2026-09-16",
    )
    assert completed[12:42] == tuple(
        f"advanced|current|{value}" for value, _ in CASE_TYPES
    )
    assert completed[-1] == "advanced|appeal|Appeal lodged"
    assert checkpoint.live_complete
    assert batches[-1].complete

    advanced_posts = [
        fields
        for method, path, fields in mock.requests
        if method == "POST" and path.endswith("/advancedSearchResults.do")
    ]
    assert len(advanced_posts) == EXPECTED_ADVANCED_QUERY_COUNT
    assert dict(advanced_posts[0])["date(applicationValidatedStart)"] == "18/08/2026"
    assert dict(advanced_posts[1])["date(applicationDecisionEnd)"] == "16/09/2026"
    assert dict(advanced_posts[2])["searchCriteria.caseStatus"] == "Current"
    assert dict(advanced_posts[2])["searchCriteria.caseType"] == "DAG"
    assert dict(advanced_posts[-1])["searchCriteria.appealStatus"] == "Appeal lodged"
    assert advanced_posts[0][-2:] == (("tag", "one"), ("tag", "two"))
    assert all(
        dict(fields)["caseAddressType"] == "Application" for fields in advanced_posts
    )


def test_leeds_rejects_advanced_case_type_taxonomy_drift() -> None:
    """A changed exhaustive partition cannot inherit the investigated proof."""
    with pytest.raises(LeedsParseError, match="case type"):
        asyncio.run(_discover(_LeedsSearchMock(case_types=CASE_TYPES[:-1])))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html></html>", "advanced form"),
        (
            _advanced_form().replace(b'method="post"', b'method="get"'),
            "advanced form",
        ),
        (
            _advanced_form().replace(
                b'<input name="searchCriteria.reference" value="">',
                b"",
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<input type="hidden" name="caseAddressType" value="Application">',
                b'<input type="hidden" name="caseAddressType" value="">',
            ),
            "advanced form discriminators",
        ),
        (
            _advanced_form().replace(
                b'<select name="searchCriteria.caseStatus">',
                (
                    b'<input name="searchCriteria.caseStatus" value="">'
                    b'<select name="other.caseStatus">'
                ),
            ),
            "advanced case status",
        ),
        (
            _advanced_form().replace(
                b'<input name="searchCriteria.reference" value="">',
                (
                    b'<input name="searchCriteria.reference" value="">'
                    b'<input name="searchCriteria.unseen" value="narrow" disabled>'
                ),
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<option value="Current">Current</option>',
                b'<option value="Current">Current</option><option>New</option>',
            ),
            "advanced case status options",
        ),
        (
            _advanced_form().replace(
                b'<input name="searchCriteria.reference" value="">',
                (
                    b'<input name="searchCriteria.reference" value="">'
                    b'<input name="searchCriteria.unseen" value="narrow">'
                ),
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<input type="hidden" name="caseAddressType" value="Application">',
                (
                    b'<input type="hidden" name="caseAddressType" '
                    b'value="Application">'
                    b'<input type="hidden" name="caseAddressType" '
                    b'value="Application">'
                ),
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<input type="hidden" name="tag" value="two">',
                (
                    b'<input type="hidden" name="tag" value="two">'
                    b'<input type="hidden" name="tag" value="three">'
                ),
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<input name="searchCriteria.reference" value="">',
                b'<input name="searchCriteria.reference" value="" disabled>',
            ),
            "advanced form fields",
        ),
        (
            _advanced_form().replace(
                b'<option value="Current">',
                b'<option value="Current" disabled>',
            ),
            "advanced case status options",
        ),
        (
            _advanced_form().replace(
                b'<select name="searchCriteria.caseStatus">',
                b'<select name="searchCriteria.caseStatus" disabled>',
            ),
            "advanced case status",
        ),
    ],
    ids=(
        "missing",
        "method",
        "field",
        "discriminator",
        "select",
        "extra-disabled-filter",
        "unvalued-option",
        "unknown-filter",
        "duplicate-discriminator",
        "extra-opaque",
        "disabled-filter",
        "disabled-option",
        "disabled-select",
    ),
)
def test_leeds_rejects_advanced_form_boundary_drift(
    body: bytes,
    message: str,
) -> None:
    """Every portal-owned advanced-form discriminator fails closed."""
    with pytest.raises(LeedsParseError, match=message):
        leeds_adapter._parse_advanced_form(body)


def test_leeds_form_fields_exclude_disabled_controls() -> None:
    """Disabled controls are never copied into a submitted Leeds request."""
    form = BeautifulSoup(
        '<form><input name="enabled" value="yes">'
        '<input name="disabled" value="no" disabled></form>',
        "html.parser",
    ).select_one("form")
    assert form is not None

    assert leeds_adapter._form_fields(form) == (FormField(name="enabled", value="yes"),)


def test_leeds_rejects_a_capped_case_type_partition() -> None:
    """A capped partition is neither empty nor complete."""
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(_discover(_LeedsSearchMock(capped_case_type="FU")))

    assert type(raised.value).__name__ == "LeedsSearchCapError"


def test_leeds_rejects_an_invalid_advanced_page_number() -> None:
    """Advanced result parsing never accepts a non-positive page."""
    with pytest.raises(LeedsParseError, match="advanced result page"):
        leeds_adapter._parse_advanced_search_page(
            b"<p>No results found</p>",
            page=0,
        )
    with pytest.raises(LeedsParseError, match="requested result page"):
        leeds_adapter._parse_search_page(
            b"<p>No results found</p>",
            expected_page=0,
        )


def test_leeds_checkpoint_rejects_unroutable_and_conflicting_identities() -> None:
    """Queued human references retain one non-empty portal keyVal."""
    advance = leeds_adapter._advance_checkpoint
    active_page = leeds_adapter._ActivePage(
        query_key="query",
        page=1,
        row_count=0,
    )
    search_page = leeds_adapter._SearchPage

    with pytest.raises(ValueError, match="no keyVal locator"):
        advance(
            LeedsCheckpointV1(result_page="live"),
            active_page=active_page,
            search_page=search_page(
                references=(
                    SourceReference(source_id=SOURCE, reference="26/05001/FU"),
                ),
                reported=1,
            ),
            all_query_keys=("query",),
        )

    checkpoint = LeedsCheckpointV1(
        result_page="live",
        seen_references=("26/05001/FU",),
        seen_identities=(
            leeds_adapter.LeedsReferenceIdentityV1(
                reference="26/05001/FU",
                locator="ORIGINAL",
            ),
        ),
    )
    with pytest.raises(LeedsParseError, match="identity conflict"):
        advance(
            checkpoint,
            active_page=active_page,
            search_page=search_page(
                references=(
                    SourceReference(
                        source_id=SOURCE,
                        reference="26/05001/FU",
                        locator="CHANGED",
                    ),
                ),
                reported=1,
            ),
            all_query_keys=("query",),
        )


def test_leeds_terminal_checkpoint_rerun_has_zero_network_io() -> None:
    """The same terminal scope returns before either form is requested."""
    first = asyncio.run(_discover(_LeedsSearchMock()))
    checkpoint = first[-1].next_checkpoint
    mock = _LeedsSearchMock(reject_requests=True)

    second = asyncio.run(_discover(mock, checkpoint))

    assert len(second) == 1
    assert second[0].complete
    assert second[0].references == ()
    assert mock.requests == []


def _advanced_result_page(reference: str, locator: str, *, page: int = 1) -> bytes:
    return f"""
    <div data-result-count="2"></div>
    <input name="searchCriteria.page" value="{page}">
    <li class="searchresult">
      <a href="applicationDetails.do?keyVal={locator}&activeTab=summary">
        <span>Reference</span><span>{reference}</span>
      </a>
    </li>
    """.encode()


class _LeedsPagedAdvancedMock(_LeedsSearchMock):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        if path.endswith("/advancedSearchResults.do"):
            values = dict(fields)
            if values.get("date(applicationValidatedStart)"):
                self.requests.append((request.method, path, fields))
                return httpx.Response(
                    200,
                    content=_advanced_result_page("26/05001/FU", "ADVANCED-A"),
                )
        if path.endswith("/pagedSearchResults.do"):
            self.requests.append((request.method, path, fields))
            return httpx.Response(
                200,
                content=_advanced_result_page(
                    "26/05002/FU",
                    "ADVANCED-B",
                    page=SECOND_PAGE,
                ),
            )
        return super().__call__(request)


def test_leeds_exhausts_a_paginated_advanced_partition() -> None:
    """Advanced discovery advances page state until its reported total."""
    mock = _LeedsPagedAdvancedMock()

    batches = asyncio.run(_discover(mock))

    assert batches[-1].complete
    assert any(
        reference.reference == "26/05002/FU"
        for batch in batches
        for reference in batch.references
    )


def test_leeds_resumes_a_paginated_advanced_partition() -> None:
    """An interrupted advanced query restores portal state before page two."""

    async def interrupt_and_resume() -> tuple[
        LeedsCheckpointV1,
        list[DiscoveryBatch[LeedsCheckpointV1]],
        _LeedsPagedAdvancedMock,
    ]:
        first_mock = _LeedsPagedAdvancedMock()
        first_session = _session(first_mock)
        batches = cast(
            "AsyncGenerator[DiscoveryBatch[LeedsCheckpointV1]]",
            LeedsAdapter().discover(first_session, WINDOW, None),
        )
        try:
            first_page = None
            for _ in range(11):
                first_page = await anext(batches)
            assert first_page is not None
            checkpoint = first_page.next_checkpoint
        finally:
            await batches.aclose()
            await first_session.aclose()

        resumed_mock = _LeedsPagedAdvancedMock()
        resumed_session = _session(resumed_mock)
        try:
            resumed = [
                batch
                async for batch in LeedsAdapter().discover(
                    resumed_session,
                    WINDOW,
                    checkpoint,
                )
            ]
        finally:
            await resumed_session.aclose()
        return checkpoint, resumed, resumed_mock

    checkpoint, resumed, mock = asyncio.run(interrupt_and_resume())

    assert checkpoint.active_query == "advanced|validated|2026-08-18|2026-09-16"
    assert checkpoint.next_page == SECOND_PAGE
    assert checkpoint.query_row_count == 1
    assert resumed[-1].complete
    assert resumed[0].references[0].reference == "26/05002/FU"
    advanced_first_pages = [
        request
        for request in mock.requests
        if request[1].endswith("/advancedSearchResults.do")
    ]
    assert len(advanced_first_pages) == EXPECTED_ADVANCED_QUERY_COUNT
    assert any(
        path.endswith("/pagedSearchResults.do")
        for _method, path, _fields in mock.requests
    )


@pytest.mark.parametrize(
    "body",
    [
        _advanced_result_page("26/05001/FU", "ADVANCED-A"),
        _advanced_result_page("26/05001/FU", "ADVANCED-A").replace(
            b'<input name="searchCriteria.page" value="1">',
            b"",
        ),
    ],
    ids=("repeated-page-one", "missing-page-marker"),
)
def test_leeds_rejects_an_unbound_advanced_result_page(body: bytes) -> None:
    """A lost portal session cannot turn an unbound page into completeness."""
    with pytest.raises(LeedsParseError, match="result page"):
        leeds_adapter._parse_advanced_search_page(
            body,
            page=SECOND_PAGE,
        )


def test_leeds_repairs_a_complete_inventory_checkpoint_flag() -> None:
    """A complete query inventory can repair a stale nonterminal flag."""
    terminal = asyncio.run(_discover(_LeedsSearchMock()))[-1].next_checkpoint
    checkpoint = terminal.model_copy(update={"live_complete": False})
    mock = _LeedsSearchMock()

    batches = asyncio.run(_discover(mock, checkpoint))

    assert len(batches) == 1
    assert batches[0].complete
    assert batches[0].next_checkpoint.live_complete
    assert [path for _method, path, _fields in mock.requests] == [
        "/online-applications/search.do",
        "/online-applications/search.do",
    ]


def _late_terminal_page() -> bytes:
    rows = "".join(
        f"""
        <li class="searchresult">
          <a href="applicationDetails.do?keyVal=LATE-{index}&activeTab=summary">
            <span>Reference</span><span>26/0500{index}/FU</span>
          </a>
        </li>
        """
        for index in range(1, 7)
    )
    return f"""
    <div class="showing">Showing 111-116 of 116</div>
    <input name="searchCriteria.page" value="1">
    <select name="searchCriteria.resultsPerPage">
      <option selected value="10">10</option>
    </select>
    <div class="pager">
      <a href="pagedSearchResults.do?action=page&amp;searchCriteria.page=11">
        Previous
      </a>
      <strong>12</strong>
    </div>
    {rows}
    <div class="showing">Showing 111-116 of 116</div>
    """.encode()


class _LeedsVisiblePageMock(_LeedsSearchMock):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pagedSearchResults.do"):
            fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
            self.requests.append((request.method, request.url.path, fields))
            return httpx.Response(200, content=_late_terminal_page())
        return super().__call__(request)


def test_leeds_uses_visible_page_when_hidden_page_marker_is_stale() -> None:
    """A canonical pager reconciles Leeds's stale hidden page-one input."""
    checkpoint = LeedsCheckpointV1(
        result_page="live",
        live_scope=LeedsDiscoveryScope(
            start=WINDOW.start,
            end=WINDOW.end,
            include_open=True,
        ),
        active_query="17/08/2026|DC_Validated",
        next_page=12,
        query_row_count=110,
    )

    async def resume_page() -> DiscoveryBatch[LeedsCheckpointV1]:
        session = _session(_LeedsVisiblePageMock())
        batches = cast(
            "AsyncGenerator[DiscoveryBatch[LeedsCheckpointV1]]",
            LeedsAdapter().discover(session, WINDOW, checkpoint),
        )
        try:
            return await anext(batches)
        finally:
            await batches.aclose()
            await session.aclose()

    batch = asyncio.run(resume_page())

    assert batch.next_checkpoint.completed_queries == ("17/08/2026|DC_Validated",)
    assert batch.next_checkpoint.query_totals == (116,)
    assert batch.next_checkpoint.active_query is None


def _summary(
    *,
    reference: str = "26/05177/TR",
    blank_optional: bool = False,
    validated_date: str = "16/09/2026",
) -> bytes:
    optional = "" if blank_optional else validated_date
    address = "" if blank_optional else "1 Park Row, Leeds"
    appeal_status = "" if blank_optional else "Appeal lodged"
    appeal_decision = "" if blank_optional else "Unknown"
    return f"""
    <table id="simpleDetailsTable">
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Application Validated</th><td>{optional}</td></tr>
      <tr><th>Address</th><td>{address}</td></tr>
      <tr><th>Proposal</th><td>Works to protected trees</td></tr>
      <tr><th>Status</th><td>Current</td></tr>
      <tr><th>Appeal Status</th><td>{appeal_status}</td></tr>
      <tr><th>Appeal Decision</th><td>{appeal_decision}</td></tr>
    </table>
    """.encode()


def _documents(
    *,
    header_only: bool = False,
    malformed: bool = False,
    structural_cell: str = "",
    compact: bool = False,
    header_drift: bool = False,
) -> bytes:
    if malformed:
        return b"<h2>Documents</h2><p>Unexpected response</p>"
    if compact:
        row = (
            ""
            if header_only
            else """
      <tr><td>15/09/2026</td><td>Plan</td>
      <td>Tree location plan</td><td><a href="files/tree-plan.pdf">View</a></td></tr>
    """
        )
        header = """
      <tr><th>Date Published</th><th>Document Type</th>
      <th>Description</th><th>View</th></tr>
    """
    else:
        row = (
            ""
            if header_only
            else f"""
      <tr><td>{structural_cell}</td><td>15/09/2026</td><td>Plan</td><td>A-01</td>
      <td>Tree location plan</td><td><a href="files/tree-plan.pdf">View</a></td></tr>
    """
        )
        header = """
      <tr><td></td><td>Date Published</td><td>Document Type</td><td>Measure</td>
      <td>Description</td><td>View</td></tr>
    """
    if header_drift:
        header = header.replace("View", "Download")
    expected = 0 if header_only else 1
    marker = (
        '<li class="nodocuments"><span>Documents (0)</span></li>'
        if expected == 0
        else (
            '<a class="active" id="tab_documents">'
            f"<span>Documents ({expected})</span></a>"
        )
    )
    return f"""
    {marker}
    <table summary="Documents">
      {header}
      {row}
    </table>
    """.encode()


class _LeedsDetailMock:
    def __init__(  # noqa: PLR0913
        self,
        *,
        reference: str = "26/05177/TR",
        blank_optional: bool = False,
        validated_date: str = "16/09/2026",
        header_only: bool = False,
        malformed_documents: bool = False,
        failed_documents: bool = False,
        transient_summary_failures: int = 0,
        structural_document_cell: bool = False,
        invalid_document_cell_text: bool = False,
        transient_document_failures: int = 0,
        compact_documents: bool = False,
        document_header_drift: bool = False,
        restricted_documents: bool = False,
    ) -> None:
        self.reference = reference
        self.blank_optional = blank_optional
        self.validated_date = validated_date
        self.header_only = header_only
        self.malformed_documents = malformed_documents
        self.failed_documents = failed_documents
        self.transient_summary_failures = transient_summary_failures
        self.structural_document_cell = structural_document_cell
        self.invalid_document_cell_text = invalid_document_cell_text
        self.transient_document_failures = transient_document_failures
        self.compact_documents = compact_documents
        self.document_header_drift = document_header_drift
        self.restricted_documents = restricted_documents
        self.tabs: list[str] = []
        self.attachment_paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        if request.url.path.endswith("/applicationDetails.do"):
            tab = request.url.params["activeTab"]
            self.tabs.append(tab)
            if tab == "summary":
                if self.transient_summary_failures:
                    self.transient_summary_failures -= 1
                    return httpx.Response(
                        200,
                        content=(
                            b"<p>Unable to perform this task. "
                            b"A remote exception occurred.</p>"
                        ),
                    )
                return httpx.Response(
                    200,
                    content=_summary(
                        reference=self.reference,
                        blank_optional=self.blank_optional,
                        validated_date=self.validated_date,
                    ),
                )
            if tab == "documents":
                if self.restricted_documents:
                    return httpx.Response(
                        200,
                        content=(
                            b"<title>Error</title><h1>Error</h1>"
                            b"<h3>Permission Denied</h3>"
                            b"<p>You do not have permission to view the page.</p>"
                        ),
                    )
                if self.transient_document_failures:
                    self.transient_document_failures -= 1
                    return httpx.Response(
                        200,
                        content=(
                            b"<p>Unable to perform this task. "
                            b"A remote exception occurred.</p>"
                        ),
                    )
                if self.failed_documents:
                    return httpx.Response(503)
                return httpx.Response(
                    200,
                    content=_documents(
                        header_only=self.header_only,
                        malformed=self.malformed_documents,
                        compact=self.compact_documents,
                        header_drift=self.document_header_drift,
                        structural_cell=(
                            '<label class="hide">Select this document</label>'
                            '<input type="checkbox" name="file" value="plan.pdf">'
                            if self.structural_document_cell
                            else "Unexpected"
                            if self.invalid_document_cell_text
                            else ""
                        ),
                    ),
                )
        if request.url.path.endswith(".pdf"):
            self.attachment_paths.append(request.url.path)
            return httpx.Response(500)
        message = f"unexpected request {request.method} {request.url}"
        raise AssertionError(message)


async def _fetch(mock: _LeedsDetailMock) -> NativeSnapshot[LeedsApplicationV1]:
    session = _session(mock)
    try:
        return await LeedsAdapter().fetch(
            session,
            SourceReference(
                source_id=SOURCE,
                reference="26/05177/TR",
                locator="TLESK5JBLRI00",
            ),
        )
    finally:
        await session.aclose()


def test_leeds_fetches_summary_and_six_cell_document_metadata() -> None:
    """Successful detail retains source fields without opening attachments."""
    mock = _LeedsDetailMock()

    snapshot = asyncio.run(_fetch(mock))
    payload = snapshot.payload

    assert payload.application_reference == "26/05177/TR"
    assert payload.validated_date == date(2026, 9, 16)
    assert payload.address == "1 Park Row, Leeds"
    assert payload.appeal_status == "Appeal lodged"
    assert payload.appeal_decision == "Unknown"
    assert payload.application_type is None
    assert len(payload.documents) == 1
    assert payload.documents[0].published_date == date(2026, 9, 15)
    assert payload.documents[0].document_type == "Plan"
    assert payload.documents[0].drawing_number == "A-01"
    assert payload.documents[0].description == "Tree location plan"
    assert str(payload.documents[0].url).endswith("/files/tree-plan.pdf")
    assert isinstance(snapshot.completeness.comments, UnavailableSection)
    assert mock.tabs == ["summary", "documents"]
    assert mock.attachment_paths == []

    normalised = LeedsAdapter().normalise(snapshot)
    assert normalised.metadata.address == "1 Park Row, Leeds"
    assert normalised.metadata.validated_date == date(2026, 9, 16)
    assert normalised.normaliser_version == "leeds-v2"


def test_leeds_accepts_the_live_document_selection_cell() -> None:
    """Accessibility text in the structural cell is not document metadata."""
    snapshot = asyncio.run(_fetch(_LeedsDetailMock(structural_document_cell=True)))

    assert len(snapshot.payload.documents) == 1
    assert snapshot.payload.documents[0].title == "Tree location plan"
    assert isinstance(snapshot.completeness.documents, CompleteSection)


def test_leeds_accepts_the_live_compact_document_table() -> None:
    """Listed-building records can omit selection and measure columns."""
    snapshot = asyncio.run(_fetch(_LeedsDetailMock(compact_documents=True)))

    assert len(snapshot.payload.documents) == 1
    assert snapshot.payload.documents[0].published_date == date(2026, 9, 15)
    assert snapshot.payload.documents[0].document_type == "Plan"
    assert snapshot.payload.documents[0].drawing_number is None
    assert snapshot.payload.documents[0].description == "Tree location plan"


def test_leeds_preserves_restricted_documents_as_unavailable() -> None:
    """The official permission-denied page is explicit source unavailability."""
    snapshot = asyncio.run(_fetch(_LeedsDetailMock(restricted_documents=True)))

    assert snapshot.payload.documents == ()
    assert isinstance(snapshot.completeness.documents, UnavailableSection)


def test_leeds_rejects_unrecognised_document_selection_text() -> None:
    """Unexpected data in the structural cell cannot shift document columns."""
    snapshot = asyncio.run(_fetch(_LeedsDetailMock(invalid_document_cell_text=True)))

    assert snapshot.payload.documents == ()
    assert isinstance(snapshot.completeness.documents, FailedSection)


def test_leeds_accepts_blank_optional_summary_and_header_only_documents() -> None:
    """Blank source fields stay absent and a proven header-only table is empty."""
    snapshot = asyncio.run(
        _fetch(_LeedsDetailMock(blank_optional=True, header_only=True))
    )

    assert snapshot.payload.validated_date is None
    assert snapshot.payload.address is None
    assert snapshot.payload.appeal_status is None
    assert snapshot.payload.appeal_decision is None
    assert snapshot.payload.documents == ()
    assert isinstance(snapshot.completeness.documents, EmptySection)


def test_leeds_accepts_observed_weekday_date_rendering() -> None:
    """The official summary's weekday-prefixed date remains typed."""
    snapshot = asyncio.run(_fetch(_LeedsDetailMock(validated_date="Wed 19 Aug 2026")))

    assert snapshot.payload.validated_date == date(2026, 8, 19)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html></html>", "simpleDetailsTable"),
        (
            b'<table id="simpleDetailsTable"><tr><td>orphan</td></tr></table>',
            "summary labelled values",
        ),
        (
            (
                b'<table id="simpleDetailsTable">'
                b"<tr><th>Reference</th><td>A</td></tr>"
                b"<tr><th>Reference</th><td>B</td></tr></table>"
            ),
            "summary labelled values",
        ),
    ],
    ids=("missing-table", "empty-fields", "duplicate-label"),
)
def test_leeds_rejects_malformed_summary_boundaries(
    body: bytes,
    message: str,
) -> None:
    """Malformed summary tables cannot create partially trusted records."""
    with pytest.raises(LeedsParseError, match=message):
        leeds_adapter._parse_summary(body)


def test_leeds_accepts_an_explicit_empty_document_page() -> None:
    """Only the official no-documents wording establishes emptiness."""
    documents, state = leeds_adapter._parse_documents(
        b'<li class="nodocuments"><span>Documents (0)</span></li>'
        b"<p>No documents found</p>"
    )

    assert documents == ()
    assert isinstance(state, EmptySection)


def test_leeds_rejects_a_contradictory_empty_document_page() -> None:
    """No-documents wording cannot overrule a nonzero authoritative count."""
    with pytest.raises(LeedsParseError, match="documents table"):
        leeds_adapter._parse_documents(
            b'<a class="active" id="tab_documents">'
            b"<span>Documents (2)</span></a><p>No documents found</p>"
        )


def test_leeds_rejects_a_truncated_document_index() -> None:
    """The displayed document total must equal every parsed metadata row."""
    truncated = _documents().replace(b"Documents (1)", b"Documents (2)")

    with pytest.raises(LeedsParseError, match="expected 2 actual 1"):
        leeds_adapter._parse_documents(truncated)


def test_leeds_accepts_the_observed_stale_zero_document_tab() -> None:
    """A Leeds no-documents tab can coexist with one complete metadata table."""
    body = _documents().replace(
        b'<a class="active" id="tab_documents"><span>Documents (1)</span></a>',
        b'<li class="nodocuments"><span>Documents (0)</span></li>',
    )

    documents, state = leeds_adapter._parse_documents(body)

    assert len(documents) == 1
    assert isinstance(state, CompleteSection)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_documents() + b'<table summary="Documents"></table>', "documents table"),
        (
            _documents().replace(
                b'<a class="active" id="tab_documents"><span>Documents (1)</span></a>',
                b"",
            ),
            "documents displayed count",
        ),
        (
            (
                b'<li class="nodocuments"><span>Documents (0)</span></li>'
                b'<table summary="Documents"></table>'
            ),
            "documents table header",
        ),
        (
            _documents().replace(b"Documents (1)", b"Files (1)"),
            "documents displayed count",
        ),
        (
            _documents(header_only=True).replace(b"Documents (0)", b"Documents (1)"),
            "documents displayed count",
        ),
        (
            _documents().replace(
                b"</table>",
                b'<a href="pagedSearchResults.do?action=page">Next</a></table>',
            ),
            "documents pagination",
        ),
        (
            (
                b'<a class="active" id="tab_documents">'
                b"<span>Documents (1)</span></a>"
                b'<table summary="Documents">'
                b"<tr><th>Date Published</th><th>Document Type</th>"
                b"<th>Description</th><th>View</th></tr>"
                b"<tr><td>15/09/2026</td><td>Plan</td><td>Description</td></tr>"
                b"</table>"
            ),
            "document metadata row",
        ),
        (
            _documents().replace(
                b'<a href="files/tree-plan.pdf">View</a>',
                b"View",
            ),
            "document metadata link",
        ),
        (
            _documents().replace(b"15/09/2026", b"not-a-date"),
            "document published date",
        ),
    ],
    ids=(
        "multiple-tables",
        "missing-count",
        "missing-header",
        "marker-label",
        "nonzero-no-documents-marker",
        "pagination",
        "row-width",
        "missing-link",
        "published-date",
    ),
)
def test_leeds_rejects_malformed_document_boundaries(
    body: bytes,
    message: str,
) -> None:
    """Every observed document-table invariant remains fail-closed."""
    with pytest.raises(LeedsParseError, match=message):
        leeds_adapter._parse_documents(body)


def test_leeds_result_pager_and_required_field_boundaries() -> None:
    """Pager state and typed summary fields reject ambiguous source values."""
    visible_page = leeds_adapter._visible_result_page
    with pytest.raises(LeedsParseError, match="reported result count"):
        visible_page(
            BeautifulSoup(
                '<div class="pager"><strong>one</strong></div>', "html.parser"
            ),
            (1, 1, 1),
        )
    with pytest.raises(LeedsParseError, match="reported result count"):
        visible_page(
            BeautifulSoup(
                '<div class="pager"><strong>1</strong></div>'
                '<select name="searchCriteria.resultsPerPage">'
                '<option selected value="many">many</option></select>',
                "html.parser",
            ),
            (1, 1, 1),
        )
    with pytest.raises(LeedsParseError, match="reported result count"):
        visible_page(
            BeautifulSoup(
                '<div class="pager"><strong>2</strong></div>'
                '<select name="searchCriteria.resultsPerPage">'
                '<option selected value="10">10</option></select>',
                "html.parser",
            ),
            (1, 1, 11),
        )

    current_pages = leeds_adapter._current_result_pages
    assert current_pages(
        BeautifulSoup(
            '<input name="searchCriteria.page" value="">',
            "html.parser",
        ),
        allow_empty_first_page_marker=True,
    ) == (1,)
    with pytest.raises(LeedsParseError, match="reported result count"):
        current_pages(
            BeautifulSoup(
                '<input name="searchCriteria.page" value="later">',
                "html.parser",
            ),
            allow_empty_first_page_marker=False,
        )

    with pytest.raises(LeedsParseError, match="summary proposal"):
        leeds_adapter._required_field({}, "proposal")
    with pytest.raises(LeedsParseError, match="date received date"):
        leeds_adapter._optional_date(
            {"received date": "not-a-date"},
            "received date",
        )
    with pytest.raises(LeedsParseError, match="comments displayed count"):
        leeds_adapter._section_count(
            BeautifulSoup("<p></p>", "html.parser"), "comments"
        )


def test_leeds_retries_a_transient_summary_shell() -> None:
    """A bounded retry recovers the portal's intermittent HTTP-200 error page."""
    mock = _LeedsDetailMock(transient_summary_failures=1)

    snapshot = asyncio.run(_fetch(mock))

    assert snapshot.payload.application_reference == "26/05177/TR"
    assert mock.tabs == ["summary", "summary", "documents"]


def test_leeds_retries_a_transient_document_shell() -> None:
    """The observed HTTP-200 remote exception is bounded and retryable."""
    mock = _LeedsDetailMock(transient_document_failures=1)

    snapshot = asyncio.run(_fetch(mock))

    assert len(snapshot.payload.documents) == 1
    assert mock.tabs == ["summary", "documents", "documents"]


def test_leeds_rejects_a_persistent_document_shell() -> None:
    """A persistent document remote exception cannot become an empty index."""
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(_fetch(_LeedsDetailMock(transient_document_failures=3)))

    assert type(raised.value).__name__ == "LeedsDetailUnavailableError"


@pytest.mark.parametrize("failure", ["malformed", "header"])
def test_leeds_preserves_document_section_failures(failure: str) -> None:
    """An unverified document index remains failed rather than empty."""
    snapshot = asyncio.run(
        _fetch(
            _LeedsDetailMock(
                malformed_documents=failure == "malformed",
                document_header_drift=failure == "header",
            )
        )
    )

    assert snapshot.payload.documents == ()
    assert isinstance(snapshot.completeness.documents, FailedSection)


def test_leeds_retries_document_transport_failures_as_a_whole_record() -> None:
    """A document transport outage propagates into the collector retry queue."""
    with pytest.raises(SourceUnavailableError):
        asyncio.run(_fetch(_LeedsDetailMock(failed_documents=True)))


def test_leeds_rejects_published_reference_disagreement() -> None:
    """The source summary cannot silently replace the queued identity."""
    with pytest.raises(ValueError, match="reference"):
        asyncio.run(_fetch(_LeedsDetailMock(reference="MISMATCH/0001")))


def _result_page(reference: str, locator: str) -> bytes:
    return f"""
    <div data-result-count="1"></div>
    <li class="searchresult">
      <a href="applicationDetails.do?keyVal={locator}&activeTab=summary">
        <span>Reference</span><span>{reference}</span>
      </a>
    </li>
    """.encode()


class _LeedsQualificationMock(_LeedsSearchMock):
    def __init__(
        self,
        *,
        failed_documents: bool = False,
        summary_shell: bool = False,
    ) -> None:
        super().__init__()
        self.failed_documents = failed_documents
        self.summary_shell = summary_shell
        self.emitted_reference = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/applicationDetails.do"):
            self.requests.append((request.method, request.url.path, ()))
            tab = request.url.params["activeTab"]
            if tab == "summary":
                if self.summary_shell:
                    return httpx.Response(
                        200,
                        content=(
                            b"<p>Unable to perform this task. "
                            b"A remote exception occurred.</p>"
                        ),
                    )
                return httpx.Response(200, content=_summary())
            if tab == "documents":
                return httpx.Response(
                    200,
                    content=_documents(malformed=self.failed_documents),
                )
        response = super().__call__(request)
        if (
            request.url.path.endswith("/weeklyListResults.do")
            and not self.emitted_reference
        ):
            self.emitted_reference = True
            return httpx.Response(
                200,
                content=_result_page("26/05177/TR", "TLESK5JBLRI00"),
            )
        return response


class _LeedsLiveWeekQualificationMock(_LeedsQualificationMock):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("/search.do")
            and request.url.params.get("action") == "weeklyList"
        ):
            fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
            self.requests.append((request.method, request.url.path, fields))
            return httpx.Response(200, content=_live_weekly_form())
        return super().__call__(request)


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_leeds.py"
    name = "_test_qualify_leeds"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _qualification_args(data_dir: Path, *, resume: bool = False) -> list[str]:
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    return [*arguments, "--resume"] if resume else arguments


def test_leeds_qualification_reparses_retained_current_evidence() -> None:
    """A pre-fix stored success cannot pass current receipt semantics."""
    module = _qualification_module()
    snapshot = asyncio.run(_fetch(_LeedsDetailMock()))
    record = RetainedNativeRecord(
        application_id=ApplicationId("leeds:test"),
        authority_id=AuthorityId("leeds"),
        reference=snapshot.reference,
        native_schema=LeedsApplicationV1.__name__,
        native_json=snapshot.payload.model_dump_json(),
        observed_at=snapshot.observed_at,
        completeness=snapshot.completeness,
        evidence=snapshot.evidence,
    )
    assert module._revalidates_current_record(record)
    assert not module._revalidates_current_record(
        record.model_copy(update={"authority_id": "cornwall"})
    )
    assert not module._revalidates_current_record(
        record.model_copy(update={"native_schema": "LeedsApplicationLegacy"})
    )
    assert not module._revalidates_current_record(
        record.model_copy(update={"evidence": snapshot.evidence[:1]})
    )
    assert not module._revalidates_current_record(
        record.model_copy(
            update={
                "reference": snapshot.reference.model_copy(update={"locator": None})
            }
        )
    )
    assert not module._revalidates_current_record(
        record.model_copy(
            update={
                "reference": snapshot.reference.model_copy(
                    update={"reference": "MISMATCH/0001"}
                )
            }
        )
    )

    truncated = _documents().replace(b"Documents (1)", b"Documents (2)")
    stale_capture = snapshot.evidence[1].model_copy(
        update={
            "body": truncated,
            "digest": hashlib.sha256(truncated).hexdigest(),
        }
    )
    stale_record = record.model_copy(
        update={"evidence": (snapshot.evidence[0], stale_capture)}
    )

    assert not module._revalidates_current_record(stale_record)
    altered_payload = snapshot.payload.model_copy(
        update={"proposal_text": "Different retained proposal"}
    )
    assert not module._revalidates_current_record(
        record.model_copy(update={"native_json": altered_payload.model_dump_json()})
    )
    assert not module._revalidates_current_record(
        record.model_copy(
            update={
                "completeness": snapshot.completeness.model_copy(
                    update={"documents": EmptySection()}
                )
            }
        )
    )


def test_leeds_qualification_requires_exact_30_day_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live proof cannot silently widen or shrink its bounded window."""
    module = _qualification_module()
    arguments = _qualification_args(tmp_path / "qualification")
    arguments[arguments.index("2026-08-18")] = "2026-08-19"

    assert module.main(arguments) == CONFIG_ERROR_EXIT
    assert "30-day" in capsys.readouterr().err


def test_leeds_qualification_writes_typed_receipt_and_zero_io_rerun(
    tmp_path: Path,
) -> None:
    """One successful command persists every same-day qualification invariant."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    mocks: list[_LeedsQualificationMock] = []

    def session_factory() -> HttpxPortalSession:
        mock = _LeedsQualificationMock()
        mocks.append(mock)
        return _session(mock)

    assert (
        module.main(_qualification_args(data_dir), session_factory=session_factory) == 0
    )

    receipt_path = data_dir / "leeds-qualification-v1.json"
    receipt = module.LeedsQualificationReceiptV1.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    assert receipt.schema_version == 1
    assert receipt.authority_id == "leeds"
    assert len(receipt.query_inventory) == EXPECTED_QUERY_COUNT
    assert len(receipt.query_totals) == EXPECTED_QUERY_COUNT
    assert receipt.counts.applications == 1
    assert receipt.durable_sets.exact
    assert receipt.durable_sets.checkpoint_count == 1
    assert receipt.evidence_integrity.revalidated_application_count == 1
    assert receipt.costs.initial.attachment_body_requests == 0
    assert receipt.costs.rerun.request_count == 0
    assert receipt.costs.rerun.transferred_bytes == 0
    assert receipt.costs.rerun.attachment_body_requests == 0
    assert [cycle.ordinal for cycle in receipt.weekly_cycles] == [1, 2]
    assert [cycle.status for cycle in receipt.weekly_cycles] == ["pending", "pending"]
    assert [cycle.due_on for cycle in receipt.weekly_cycles] == [
        date(2026, 9, 23),
        date(2026, 9, 30),
    ]
    assert not receipt.live_ready_promoted
    assert all(check.ok for check in receipt.checks)
    assert len(mocks) == EXPECTED_QUALIFICATION_PASSES
    assert mocks[1].requests == []

    raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["weekly_cycles"][0]["status"] == "pending"


def test_leeds_qualification_accepts_live_week_rendering(tmp_path: Path) -> None:
    """The receipt accepts the canonical textual week keys emitted by Leeds."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"

    def session_factory() -> HttpxPortalSession:
        return _session(_LeedsLiveWeekQualificationMock())

    assert (
        module.main(_qualification_args(data_dir), session_factory=session_factory) == 0
    )
    receipt = module.LeedsQualificationReceiptV1.model_validate_json(
        (data_dir / "leeds-qualification-v1.json").read_text(encoding="utf-8")
    )
    assert receipt.query_inventory[:2] == (
        "17 Aug 2026|DC_Validated",
        "17 Aug 2026|DC_Decided",
    )
    assert all(check.ok for check in receipt.checks)


def test_leeds_qualification_rejects_failed_current_sections(tmp_path: Path) -> None:
    """A malformed current document section prevents a success receipt."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"

    def session_factory() -> HttpxPortalSession:
        return _session(_LeedsQualificationMock(failed_documents=True))

    assert (
        module.main(_qualification_args(data_dir), session_factory=session_factory) == 1
    )
    assert not (data_dir / "leeds-qualification-v1.json").exists()


def test_leeds_qualification_recovers_a_bounded_transient_detail(
    tmp_path: Path,
) -> None:
    """A fresh session resumes one durable transient failure before receipt proof."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    sessions = 0

    def session_factory() -> HttpxPortalSession:
        nonlocal sessions
        sessions += 1
        return _session(_LeedsQualificationMock(summary_shell=sessions == 1))

    assert (
        module.main(_qualification_args(data_dir), session_factory=session_factory) == 0
    )
    receipt = module.LeedsQualificationReceiptV1.model_validate_json(
        (data_dir / "leeds-qualification-v1.json").read_text(encoding="utf-8")
    )
    assert receipt.run_statuses[-2:] == ("succeeded", "succeeded")
    assert "failed" in receipt.run_statuses
    assert all(check.ok for check in receipt.checks)
