# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, C901, E501, PLR0912, PLR0915, PLR2004, SLF001

"""Dorset Council public-register contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl

import httpx
import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl

from yimby import AuthorityId, DiscoveryWindow, pilot_registry
from yimby.authorities.dorset import adapter as dorset_adapter
from yimby.domain import (
    DurableDiscoveryBatch,
    SourceId,
    SourceReference,
    StoredCheckpoint,
)
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.transport import FormField

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import ModuleType

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)
BASE_URL = "https://planning.dorsetcouncil.gov.uk"
ADVANCED_PATH = "/advsearch.aspx"
RESULTS_PATH = "/searchresults.aspx"
DISCLAIMER_PATH = "/disclaimer.aspx"
NEXT_BUTTON = "ctl00$ContentPlaceHolder1$lvResults$RadDataPager1$ctl02$NextButton"
CALENDAR_ACTIVE_DATES = "[[1980,1,1],[2099,12,30],[2026,9,16]]"
NUMERIC_CLIENT_STATE = (
    '{"enabled":true,"emptyMessage":"","validationText":"","valueAsString":"",'
    '"minValue":-70368744177664,"maxValue":70368744177664,'
    '"lastSetTextBoxValue":""}'
)


def _date_client_state(iso_value: str = "", display_value: str = "") -> str:
    validation = f"{iso_value}-00-00-00" if iso_value else ""
    return json.dumps(
        {
            "enabled": True,
            "emptyMessage": "",
            "validationText": validation,
            "valueAsString": validation,
            "minDateStr": "1980-01-01-00-00-00",
            "maxDateStr": "2099-12-31-00-00-00",
            "lastSetTextBoxValue": display_value,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class _Result:
    reference: str
    recno: int


RECEIVED = tuple(
    _Result(f"P/FUL/2026/{index:05d}", 430_000 + index) for index in range(1, 12)
)
OUTSTANDING = (
    RECEIVED[0],
    *(_Result(f"P/OUT/2025/{index:05d}", 420_000 + index) for index in range(1, 11)),
)


def _disclaimer_form(return_url: str = "%2f") -> bytes:
    return f"""
    <form method="post" action="./disclaimer.aspx?returnURL={return_url}">
      <input type="hidden" name="__VIEWSTATE" value="disclaimer-state">
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="758A299B">
      <input type="hidden" name="__EVENTVALIDATION" value="validation-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnAccept" value="Accept">
    </form>
    """.encode()


def _advanced_form() -> bytes:
    return b"""
    <form method="post" action="./advsearch.aspx">
      <input type="hidden" name="__VIEWSTATE" value="advanced-state">
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="F6350304">
      <input type="hidden" name="__EVENTVALIDATION" value="validation-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="checkbox" name="ctl00$ContentPlaceHolder1$chkOutstanding" value="on">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtEasting" value="">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtEasting_ClientState">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnSearch2" value="Search">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedFrom" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput" value="">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedFrom_dateInput_ClientState">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_SD" value="[]">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_AD" value="[[1980,1,1],[2099,12,30],[2026,9,16]]">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedFrom_ClientState">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedTo" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput" value="">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedTo_dateInput_ClientState">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_SD" value="[]">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_AD" value="[[1980,1,1],[2099,12,30],[2026,9,16]]">
      <input type="hidden" name="ctl00_ContentPlaceHolder1_txtDateReceivedTo_ClientState">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnSearch3" value="Search">
      <input type="text" name="after-submit" value="">
    </form>
    """


def _result_page(query: str, page: int, fault: str | None = None) -> bytes:
    rows = RECEIVED if query == "received-valid" else OUTSTANDING
    page_rows = list(rows[(page - 1) * 10 : page * 10])
    if fault == "duplicate-reference" and query == "received-valid" and page == 1:
        page_rows[-1] = page_rows[0]
    if fault == "duplicate-locator" and query == "received-valid" and page == 1:
        page_rows[-1] = _Result(page_rows[-1].reference, page_rows[0].recno)
    if fault == "underfull" and query == "received-valid" and page == 1:
        page_rows.pop()
    rendered_rows = "".join(
        f"""
        <div class="result">
          <a id="ctl00_ContentPlaceHolder1_lvResults_ctrl{index}_hypDisplayRecord"
             href="plandisp.aspx?recno={row.recno}">{row.reference}</a>
          <a id="ctl00_ContentPlaceHolder1_lvResults_ctrl{index}_hypDisplayRecord2"
             href="plandisp.aspx?recno={row.recno}">View this application</a>
        </div>
        """
        for index, row in enumerate(page_rows)
    )
    has_next = page == 1 or fault == "terminal-next"
    next_controls = (
        f"""
        <input type="submit" name="{NEXT_BUTTON}" value=" ">
        <input type="submit"
          name="ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton"
          value=" ">
        """
        if has_next
        else ""
    )
    bottom_page = page + 1 if fault == "pager-mismatch" else page
    viewstate = "" if fault == "empty-viewstate" else f"{query}-page-{page}"
    return f"""
    <form method="post" action="./searchresults.aspx">
      <input type="hidden" name="__VIEWSTATE" value="{viewstate}">
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="216AC575">
      <input type="hidden" name="__EVENTVALIDATION" value="validation-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <div id="ctl00_ContentPlaceHolder1_lvResults_RadDataPager1">
        <span>Page {page} of 2</span>
        <a class="rdpCurrentPage">{page}</a>
        {next_controls}
      </div>
      {rendered_rows}
      <div id="ctl00_ContentPlaceHolder1_lvResults_pager">
        <span>Page {bottom_page} of 2</span>
        <a class="rdpCurrentPage">{bottom_page}</a>
      </div>
    </form>
    """.encode()


def _detail_page(
    reference: str,
    *,
    document_count: int = 2,
    duplicate_documents: bool = False,
) -> bytes:
    metadata = (
        (0, "20/08/2026", "Application Form - Without Personal Data", "620kb"),
        (
            1,
            "20/08/2026" if duplicate_documents else "21/08/2026",
            (
                "Application Form - Without Personal Data"
                if duplicate_documents
                else "Location Plan"
            ),
            "620kb" if duplicate_documents else "1mb",
        ),
    )
    rows = "".join(
        f"""
        <tr id="ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00__{index}">
          <td></td>
          <td><a id="document-{index}" href="#"
            onclick="return RowClicked({index}); ">{published} - {title}</a>
            ({size})</td>
        </tr>
        """
        for index, published, title, size in metadata[:document_count]
    )
    if document_count == 0:
        rows = """
        <tr class="rgNoRecords">
          <td colspan="2" style="text-align:left;">
            <div><br><strong>There are currently no scanned documents for this application.</strong></div>
          </td>
        </tr>
        """
    return f"""
    <div id="ctl00_ContentPlaceHolder1_pvDetails">
      <span class="applabel">Application No</span><p class="appdata">{reference}</p>
      <span class="applabel">Status</span><p class="appdata">Out To Consultation</p>
      <span class="applabel">Type</span><p class="appdata">Full Planning Application</p>
      <span class="applabel">Proposal</span><p class="appdata">Build two homes &amp; plant four trees</p>
      <span class="applabel">Valid Date</span><p class="appdata">15/09/2026</p>
      <span class="applabel">Decision</span><p class="appdata"></p>
      <span class="applabel">Authority</span><p class="appdata"></p>
    </div>
    <div id="ctl00_ContentPlaceHolder1_pvLocation">
      <span class="applabel">Address</span><p class="appdata">1 High Street, Dorset</p>
      <span class="applabel">Easting</span><p class="appdata">406008</p>
      <span class="applabel">Northing</span><p class="appdata">100386</p>
      <span class="applabel">Ward</span><p class="appdata">Ferndown North Ward</p>
      <span class="applabel">Parish</span><p class="appdata">Ferndown Town</p>
    </div>
    <table id="ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00">
      <thead><tr><th>Document</th><th>Size</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <script>
      var grid = {{"_gridTableViewsData":"[{{\\\"PageCount\\\":1,\\\"AllowPaging\\\":false,\\\"VirtualItemCount\\\":{document_count}}}]"}};
      function RowClicked(index) {{ return index; }}
    </script>
    """.encode()


class _DorsetMock:
    def __init__(self, *, fault: str | None = None) -> None:
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.query: str | None = None
        self.page = 0
        self.fault = fault

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        accepted = "dorset-planning=accepted" in request.headers.get("cookie", "")
        if request.method == "GET" and request.url.path == ADVANCED_PATH:
            return httpx.Response(
                200,
                content=(
                    _advanced_form()
                    if accepted
                    else _disclaimer_form(
                        "%2fadvsearch.aspx%3fAspxAutoDetectCookieSupport%3d1"
                    )
                ),
            )
        if request.method == "POST" and request.url.path == DISCLAIMER_PATH:
            assert fields == (
                ("__VIEWSTATE", "disclaimer-state"),
                ("__VIEWSTATEGENERATOR", "758A299B"),
                ("__EVENTVALIDATION", "validation-state"),
                ("tag", "one"),
                ("tag", "two"),
                ("ctl00$ContentPlaceHolder1$btnAccept", "Accept"),
            )
            return httpx.Response(
                200,
                content=b"accepted",
                headers={"set-cookie": "dorset-planning=accepted; Path=/"},
            )
        if request.method == "GET" and request.url.path == "/plandisp.aspx":
            if not accepted:
                return httpx.Response(
                    200,
                    content=_disclaimer_form(
                        "%2fplandisp.aspx%3frecno%3d430001"
                        "%26AspxAutoDetectCookieSupport%3d1"
                    ),
                )
            recno = int(request.url.params["recno"])
            reference = next(
                row.reference for row in (*RECEIVED, *OUTSTANDING) if row.recno == recno
            )
            count = 1 if self.fault == "document-count" else 2
            body = _detail_page(
                reference,
                document_count=count,
                duplicate_documents=self.fault == "duplicate-document-metadata",
            )
            if self.fault == "document-count":
                body = body.replace(
                    b'\\"VirtualItemCount\\":1',
                    b'\\"VirtualItemCount\\":2',
                )
            return httpx.Response(
                200,
                content=body,
            )
        if request.method == "POST" and request.url.path == ADVANCED_PATH:
            assert accepted
            names = {name for name, _value in fields}
            self.query = (
                "received-valid"
                if "ctl00$ContentPlaceHolder1$btnSearch3" in names
                else "outstanding"
            )
            self.page = 1
            return httpx.Response(
                200,
                content=_result_page(self.query, self.page, self.fault),
            )
        if request.method == "POST" and request.url.path == RESULTS_PATH:
            assert accepted
            assert self.query is not None
            assert fields[-1] == (NEXT_BUTTON, " ")
            self.page += 1
            return httpx.Response(
                200,
                content=_result_page(self.query, self.page, self.fault),
            )
        message = f"unexpected request {request.method} {request.url}"
        raise AssertionError(message)


def _session(mock: Any) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )


def _pairs(mock: _DorsetMock, path: str) -> list[tuple[tuple[str, str], ...]]:
    return [
        fields
        for method, request_path, fields in mock.requests
        if method == "POST" and request_path == path
    ]


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_dorset.py"
    name = "_test_qualify_dorset"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_dorset_live_discovery_replays_exact_forms_and_exhausts_both_queries() -> None:
    """The public package proves both captured searches and every advertised page."""
    package = pilot_registry().get(AuthorityId("dorset"))
    mock = _DorsetMock()
    session = _session(mock)

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [batch async for batch in package.discover(session, WINDOW, None)]
        await session.aclose()
        return batches

    batches = asyncio.run(discover_all())

    assert [len(batch.references) for batch in batches] == [10, 1, 9, 1]
    assert batches[-1].complete
    assert [
        (reference.reference, reference.locator)
        for batch in batches
        for reference in batch.references
    ] == [(row.reference, str(row.recno)) for row in (*RECEIVED, *OUTSTANDING[1:])]
    assert _pairs(mock, DISCLAIMER_PATH) == [
        (
            ("__VIEWSTATE", "disclaimer-state"),
            ("__VIEWSTATEGENERATOR", "758A299B"),
            ("__EVENTVALIDATION", "validation-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$btnAccept", "Accept"),
        )
    ]
    assert _pairs(mock, ADVANCED_PATH) == [
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "advanced-state"),
            ("__VIEWSTATEGENERATOR", "F6350304"),
            ("__EVENTVALIDATION", "validation-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$txtEasting", ""),
            ("ctl00_ContentPlaceHolder1_txtEasting_ClientState", NUMERIC_CLIENT_STATE),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom", "2026-08-18"),
            (
                "ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput",
                "18/08/2026",
            ),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedFrom_dateInput_ClientState",
                _date_client_state("2026-08-18", "18/08/2026"),
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_SD", "[]"),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_AD",
                CALENDAR_ACTIVE_DATES,
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedFrom_ClientState", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo", "2026-09-16"),
            (
                "ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput",
                "16/09/2026",
            ),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedTo_dateInput_ClientState",
                _date_client_state("2026-09-16", "16/09/2026"),
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_SD", "[]"),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_AD",
                CALENDAR_ACTIVE_DATES,
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedTo_ClientState", ""),
            ("ctl00$ContentPlaceHolder1$btnSearch3", "Search"),
            ("after-submit", ""),
        ),
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "advanced-state"),
            ("__VIEWSTATEGENERATOR", "F6350304"),
            ("__EVENTVALIDATION", "validation-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$chkOutstanding", "on"),
            ("ctl00$ContentPlaceHolder1$txtEasting", ""),
            ("ctl00_ContentPlaceHolder1_txtEasting_ClientState", NUMERIC_CLIENT_STATE),
            ("ctl00$ContentPlaceHolder1$btnSearch2", "Search"),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput", ""),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedFrom_dateInput_ClientState",
                _date_client_state(),
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_SD", "[]"),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedFrom_calendar_AD",
                CALENDAR_ACTIVE_DATES,
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedFrom_ClientState", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput", ""),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedTo_dateInput_ClientState",
                _date_client_state(),
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_SD", "[]"),
            (
                "ctl00_ContentPlaceHolder1_txtDateReceivedTo_calendar_AD",
                CALENDAR_ACTIVE_DATES,
            ),
            ("ctl00_ContentPlaceHolder1_txtDateReceivedTo_ClientState", ""),
            ("after-submit", ""),
        ),
    ]
    assert _pairs(mock, RESULTS_PATH) == [
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "received-valid-page-1"),
            ("__VIEWSTATEGENERATOR", "216AC575"),
            ("__EVENTVALIDATION", "validation-state"),
            ("tag", "one"),
            ("tag", "two"),
            (NEXT_BUTTON, " "),
        ),
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "outstanding-page-1"),
            ("__VIEWSTATEGENERATOR", "216AC575"),
            ("__EVENTVALIDATION", "validation-state"),
            ("tag", "one"),
            ("tag", "two"),
            (NEXT_BUTTON, " "),
        ),
    ]

    checkpoint = json.loads(batches[-1].next_checkpoint.payload_json)
    assert checkpoint["live_complete"] is True
    assert checkpoint["completed_queries"] == ["received-valid", "outstanding"]
    assert checkpoint["active_query"] is None
    assert checkpoint["live_scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert checkpoint["seen_references"] == [
        {
            "source_id": "dorset-planning-register",
            "reference": row.reference,
            "locator": str(row.recno),
        }
        for row in (*RECEIVED, *OUTSTANDING[1:])
    ]

    terminal_mock = _DorsetMock()
    terminal_session = _session(terminal_mock)

    async def repeat_terminal() -> list[DurableDiscoveryBatch]:
        result = [
            batch
            async for batch in package.discover(
                terminal_session,
                WINDOW,
                batches[-1].next_checkpoint,
            )
        ]
        await terminal_session.aclose()
        return result

    assert asyncio.run(repeat_terminal()) == [
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=batches[-1].next_checkpoint,
            complete=True,
        )
    ]
    assert terminal_mock.requests == []


def test_dorset_live_detail_accepts_disclaimer_and_retains_document_metadata() -> None:
    """A queued detail can establish consent without invoking a document action."""
    package = pilot_registry().get(AuthorityId("dorset"))
    mock = _DorsetMock()
    session = _session(mock)
    reference = SourceReference(
        source_id=SourceId("dorset-planning-register"),
        reference=RECEIVED[0].reference,
        locator=str(RECEIVED[0].recno),
    )

    async def collect_detail() -> Any:
        collected = await package.collect(session, reference)
        await session.aclose()
        return collected

    collected = asyncio.run(collect_detail())

    assert collected.normalised.reference == reference
    assert collected.normalised.proposal == "Build two homes & plant four trees"
    assert collected.normalised.status == "out-to-consultation"
    assert collected.normalised.metadata.application_type == "Full Planning Application"
    assert collected.normalised.metadata.decision is None
    assert collected.normalised.metadata.address == "1 High Street, Dorset"
    assert collected.normalised.metadata.validated_date == date(2026, 9, 15)
    assert collected.normalised.metadata.location is not None
    assert collected.normalised.metadata.location.bng_easting == 406_008
    assert collected.normalised.metadata.location.bng_northing == 100_386
    assert [document.title for document in collected.normalised.documents] == [
        "20/08/2026 - Application Form - Without Personal Data (620kb)",
        "21/08/2026 - Location Plan (1mb)",
    ]
    assert {str(document.url) for document in collected.normalised.documents} == {
        f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}#document-0",
        f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}#document-1",
    }
    assert not {
        str(document.url) for document in collected.normalised.documents
    }.intersection(session.requested_urls)
    assert collected.normalised.completeness.application.kind == "complete"
    assert collected.normalised.completeness.documents.kind == "complete"
    assert collected.normalised.completeness.documents.item_count == 2
    assert collected.normalised.completeness.comments.kind == "unavailable"
    assert "public comment text" in collected.normalised.completeness.comments.reason
    assert "Application Form - Without Personal Data" in collected.native_json
    assert len(collected.evidence) == 1
    assert str(collected.evidence[0].url) == (
        f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}"
    )
    assert session.attachment_body_requests == 0
    assert all(
        "DocumentsGrid" not in dict(fields)
        for method, _path, fields in mock.requests
        if method == "POST"
    )


def test_dorset_live_detail_distinguishes_duplicate_document_metadata() -> None:
    """Official table indices keep otherwise identical metadata rows distinct."""
    package = pilot_registry().get(AuthorityId("dorset"))
    session = _session(_DorsetMock(fault="duplicate-document-metadata"))

    async def collect_detail() -> Any:
        collected = await package.collect(session, _live_reference())
        await session.aclose()
        return collected

    collected = asyncio.run(collect_detail())

    assert len({document.title for document in collected.normalised.documents}) == 1
    assert [
        str(document.url).rsplit("#", 1)[-1]
        for document in collected.normalised.documents
    ] == [
        "document-0",
        "document-1",
    ]
    assert session.attachment_body_requests == 0


def test_dorset_live_resume_replays_committed_page_after_detail_consent() -> None:
    """A fresh session collects queued detail, then replays committed page proof."""
    package = pilot_registry().get(AuthorityId("dorset"))
    first_session = _session(_DorsetMock())

    async def first_page() -> DurableDiscoveryBatch:
        discovery = cast(
            "AsyncGenerator[DurableDiscoveryBatch]",
            package.discover(first_session, WINDOW, None),
        )
        try:
            return await anext(discovery)
        finally:
            await discovery.aclose()
            await first_session.aclose()

    first = asyncio.run(first_page())
    resume_mock = _DorsetMock()
    resume_session = _session(resume_mock)

    async def resume_after_detail() -> list[DurableDiscoveryBatch]:
        await package.collect(resume_session, first.references[0])
        batches = [
            batch
            async for batch in package.discover(
                resume_session,
                WINDOW,
                first.next_checkpoint,
            )
        ]
        await resume_session.aclose()
        return batches

    resumed = asyncio.run(resume_after_detail())

    assert [len(batch.references) for batch in resumed] == [1, 9, 1]
    assert resumed[-1].complete
    assert len(_pairs(resume_mock, DISCLAIMER_PATH)) == 1
    assert len(_pairs(resume_mock, RESULTS_PATH)) == 2


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("duplicate-reference", "duplicate result"),
        ("duplicate-locator", "duplicate result"),
        ("underfull", "nonterminal result rows"),
        ("pager-mismatch", "page markers"),
        ("empty-viewstate", "result form viewstate"),
        ("terminal-next", "next page"),
    ],
)
def test_dorset_live_discovery_fails_closed(fault: str, message: str) -> None:
    """Malformed page evidence never advances the durable checkpoint."""
    package = pilot_registry().get(AuthorityId("dorset"))
    session = _session(_DorsetMock(fault=fault))

    async def discover_all() -> None:
        with pytest.raises(ValueError, match=message):
            async for _batch in package.discover(session, WINDOW, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())


def test_dorset_live_detail_rejects_incomplete_document_grid() -> None:
    """Document-grid count disagreement is not represented as completeness."""
    package = pilot_registry().get(AuthorityId("dorset"))
    session = _session(_DorsetMock(fault="document-count"))
    reference = SourceReference(
        source_id=SourceId("dorset-planning-register"),
        reference=RECEIVED[0].reference,
        locator=str(RECEIVED[0].recno),
    )

    async def collect_detail() -> None:
        with pytest.raises(ValueError, match="document grid"):
            await package.collect(session, reference)
        await session.aclose()

    asyncio.run(collect_detail())


def test_dorset_qualification_persists_exact_terminal_receipt(tmp_path: Path) -> None:
    """The dated command proves durable identity, evidence, and zero-fetch replay."""
    module = _qualification_module()
    mocks: list[_DorsetMock] = []

    def session_factory() -> HttpxPortalSession:
        mock = _DorsetMock()
        mocks.append(mock)
        return _session(mock)

    fixed_now = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)
    exit_code = module.main(
        [
            "--confirm-live",
            "--include-open",
            "--data-dir",
            str(tmp_path),
        ],
        session_factory=session_factory,
        now=lambda: fixed_now,
    )

    assert exit_code == 0
    receipt_path = tmp_path / "dorset-qualification-v1.json"
    receipt = module.DorsetQualificationReceiptV1.model_validate_json(
        receipt_path.read_text()
    )
    assert receipt.created_at == fixed_now
    assert receipt.scope.model_dump(mode="json") == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert receipt.query_inventory == ("received-valid", "outstanding")
    assert receipt.terminal_checkpoint.live_complete
    assert receipt.terminal_checkpoint.completed_queries == receipt.query_inventory
    assert receipt.terminal_checkpoint.active_query is None
    assert receipt.terminal_checkpoint.next_page == 1
    assert receipt.terminal_checkpoint.total_pages is None
    assert receipt.terminal_checkpoint.active_references == ()
    assert len(receipt.terminal_checkpoint.seen_references) == 21
    assert receipt.reference_agreement.count == 21
    assert (
        len(
            {
                receipt.reference_agreement.checkpoint_sha256,
                receipt.reference_agreement.durable_queue_sha256,
                receipt.reference_agreement.applications_sha256,
            }
        )
        == 1
    )
    assert receipt.counts.model_dump() == {
        "applications": 21,
        "discovered_references": 21,
        "native_versions": 21,
        "application_versions": 21,
        "document_versions": 21,
        "comment_versions": 0,
        "pending_retries": 0,
        "failed_sections": 0,
        "unmapped_records": 0,
    }
    assert receipt.costs.initial.fetch_calls == 29
    assert receipt.costs.initial.successful_requests == 29
    assert receipt.costs.initial.transferred_bytes > 0
    assert receipt.costs.initial.attachment_body_requests == 0
    assert receipt.costs.rerun.model_dump() == {
        "fetch_calls": 0,
        "successful_requests": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt.evidence.records == 21
    assert receipt.evidence.unique_digests == 21
    assert receipt.evidence.decompressed == 21
    assert receipt.evidence.digest_matches == 21
    assert receipt.evidence.failed_digests == ()
    assert receipt.run_statuses == ("succeeded", "succeeded")
    assert all(check.ok for check in receipt.checks)
    assert [cycle.model_dump(mode="json") for cycle in receipt.weekly_cycles] == [
        {"sequence": 1, "due_on": "2026-09-23", "status": "pending"},
        {"sequence": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert receipt.readiness == "discovery-only"
    assert receipt.http_max_attempts == 1
    assert len(mocks) == 2
    assert mocks[0].requests
    assert mocks[1].requests == []
    assert not list(tmp_path.glob(".*.tmp"))


def test_dorset_qualification_fails_closed_on_corrupt_evidence(tmp_path: Path) -> None:
    """A terminal checkpoint cannot hide a corrupt retained gzip member."""
    module = _qualification_module()

    def session_factory() -> HttpxPortalSession:
        return _session(_DorsetMock())

    arguments = [
        "--confirm-live",
        "--include-open",
        "--data-dir",
        str(tmp_path),
    ]
    assert module.main(arguments, session_factory=session_factory) == 0
    receipt_path = tmp_path / "dorset-qualification-v1.json"
    receipt_path.unlink()
    evidence_path = next((tmp_path / "evidence").rglob("*.gz"))
    evidence_path.write_bytes(b"not-gzip")

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=session_factory,
        )
        == 1
    )
    assert not receipt_path.exists()


def test_dorset_qualification_restarts_stale_partial_discovery(tmp_path: Path) -> None:
    """An explicit restart replaces only stale query progress and requalifies."""
    module = _qualification_module()
    arguments = [
        "--confirm-live",
        "--include-open",
        "--data-dir",
        str(tmp_path),
    ]

    assert (
        module.main(
            arguments,
            session_factory=lambda: _session(_DorsetMock(fault="document-count")),
        )
        == 1
    )
    assert (
        module.main(
            [*arguments, "--resume", "--restart-discovery"],
            session_factory=lambda: _session(_DorsetMock()),
        )
        == 0
    )

    receipt = module.DorsetQualificationReceiptV1.model_validate_json(
        (tmp_path / "dorset-qualification-v1.json").read_text()
    )
    assert receipt.terminal_checkpoint.live_complete
    assert receipt.reference_agreement.count == 21
    assert receipt.counts.pending_retries == 0
    assert receipt.run_statuses == ("succeeded", "succeeded")


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ((), "confirmation-required"),
        (("--confirm-live",), "include-open-required"),
    ],
)
def test_dorset_qualification_requires_explicit_live_scope(
    arguments: tuple[str, ...],
    error: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live command cannot silently weaken confirmation or open discovery."""
    module = _qualification_module()
    exit_code = module.main([*arguments, "--data-dir", str(tmp_path)])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {"error": error}


def _live_reference(row: _Result = RECEIVED[0]) -> SourceReference:
    return SourceReference(
        source_id=SourceId("dorset-planning-register"),
        reference=row.reference,
        locator=str(row.recno),
    )


def _form(body: bytes) -> Tag:
    form = BeautifulSoup(body, "html.parser").select_one("form")
    assert isinstance(form, Tag)
    return form


def _mutated(body: bytes, mutation: Any) -> bytes:
    soup = BeautifulSoup(body, "html.parser")
    mutation(soup)
    return str(soup).encode()


def _three_page_result(page: int, rows: tuple[_Result, ...]) -> bytes:
    rendered_rows = "".join(
        f'<a id="row-{index}_hypDisplayRecord" '
        f'href="plandisp.aspx?recno={row.recno}">{row.reference}</a>'
        for index, row in enumerate(rows)
    )
    next_controls = (
        f'<input type="submit" name="{NEXT_BUTTON}" value=" ">'
        '<input type="submit" '
        'name="ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton" '
        'value=" ">'
        if page < 3
        else ""
    )
    return f"""
    <form method="post" action="./searchresults.aspx">
      <input type="hidden" name="__VIEWSTATE" value="page-{page}">
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="216AC575">
      <input type="hidden" name="__EVENTVALIDATION" value="validation-state">
      <div id="ctl00_ContentPlaceHolder1_lvResults_RadDataPager1">
        <span>Page {page} of 3</span><a class="rdpCurrentPage">{page}</a>
        {next_controls}
      </div>
      {rendered_rows}
      <div id="ctl00_ContentPlaceHolder1_lvResults_pager">
        <span>Page {page} of 3</span><a class="rdpCurrentPage">{page}</a>
      </div>
    </form>
    """.encode()


def test_dorset_live_received_only_and_incomplete_terminal_checkpoints() -> None:
    """The live state machine narrows queries and seals a fully replayed checkpoint."""
    package = pilot_registry().get(AuthorityId("dorset"))
    received_only = DiscoveryWindow(
        start=WINDOW.start,
        end=WINDOW.end,
        include_open=False,
    )
    first_mock = _DorsetMock()
    first_session = _session(first_mock)

    async def received_batches() -> list[DurableDiscoveryBatch]:
        batches = [
            batch
            async for batch in package.discover(first_session, received_only, None)
        ]
        await first_session.aclose()
        return batches

    batches = asyncio.run(received_batches())
    assert [len(batch.references) for batch in batches] == [10, 1]
    assert len(_pairs(first_mock, ADVANCED_PATH)) == 1

    checkpoint = dorset_adapter.DorsetCheckpointV1(
        object_offset="live",
        live_scope=dorset_adapter.DorsetDiscoveryScope(
            start=WINDOW.start,
            end=WINDOW.end,
            include_open=True,
        ),
        completed_queries=("received-valid", "outstanding"),
    )
    terminal_mock = _DorsetMock()
    terminal_session = _session(terminal_mock)

    async def seal_terminal() -> list[DurableDiscoveryBatch]:
        result = [
            batch
            async for batch in package.discover(
                terminal_session,
                WINDOW,
                StoredCheckpoint(
                    schema_version=1,
                    payload_json=checkpoint.model_dump_json(),
                ),
            )
        ]
        await terminal_session.aclose()
        return result

    terminal = asyncio.run(seal_terminal())
    assert len(terminal) == 1
    assert terminal[0].complete
    assert terminal_mock.requests == []


def test_dorset_live_rejects_query_outside_scope_and_invalid_locator() -> None:
    """Scope and locator mismatches fail before a portal request can be issued."""
    package = pilot_registry().get(AuthorityId("dorset"))
    received_only = DiscoveryWindow(
        start=WINDOW.start,
        end=WINDOW.end,
        include_open=False,
    )
    checkpoint = dorset_adapter.DorsetCheckpointV1(
        object_offset="live",
        live_scope=dorset_adapter.DorsetDiscoveryScope(
            start=WINDOW.start,
            end=WINDOW.end,
            include_open=False,
        ),
        active_query="outstanding",
    )
    session = _session(_DorsetMock())

    async def reject() -> None:
        with pytest.raises(ValueError, match="active query"):
            async for _batch in package.discover(
                session,
                received_only,
                StoredCheckpoint(
                    schema_version=1,
                    payload_json=checkpoint.model_dump_json(),
                ),
            ):
                pass
        with pytest.raises(ValueError, match="detail recno"):
            await package.collect(
                session,
                SourceReference(
                    source_id=SourceId("dorset-planning-register"),
                    reference="P/BAD/1",
                    locator="not-a-number",
                ),
            )
        await session.aclose()

    asyncio.run(reject())


@pytest.mark.parametrize(
    "progress",
    [
        dorset_adapter.DorsetCheckpointV1(completed_queries=("outstanding",)),
        dorset_adapter.DorsetCheckpointV1(
            seen_references=(
                _live_reference(),
                _live_reference().model_copy(update={"locator": "999"}),
            )
        ),
        dorset_adapter.DorsetCheckpointV1(next_page=2),
        dorset_adapter.DorsetCheckpointV1(active_query="outstanding"),
    ],
)
def test_dorset_checkpoint_validation_rejects_inconsistent_progress(
    progress: dorset_adapter.DorsetCheckpointV1,
) -> None:
    """Every contradictory saved-state shape is rejected before replay."""
    with pytest.raises(dorset_adapter.DorsetCheckpointError):
        dorset_adapter._validate_progress(
            progress,
            ("received-valid", "outstanding"),
        )


def test_dorset_checkpoint_validation_accepts_repeated_identical_seen_reference() -> (
    None
):
    """An identical repeated sighting does not fabricate a locator conflict."""
    reference = _live_reference()
    progress = dorset_adapter.DorsetCheckpointV1(
        seen_references=(reference, reference),
    )
    dorset_adapter._validate_progress(progress, ("received-valid", "outstanding"))


@pytest.mark.parametrize(
    ("progress", "page", "message"),
    [
        (
            dorset_adapter.DorsetCheckpointV1(
                active_query="received-valid",
                next_page=2,
                total_pages=3,
            ),
            dorset_adapter._ResultPage(
                references=(), page=2, total_pages=2, form=(), next_allowed=False
            ),
            "active page",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(active_query="outstanding"),
            dorset_adapter._ResultPage(
                references=(), page=1, total_pages=1, form=(), next_allowed=False
            ),
            "active query",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(seen_references=(_live_reference(),)),
            dorset_adapter._ResultPage(
                references=(_live_reference().model_copy(update={"locator": "999"}),),
                page=1,
                total_pages=1,
                form=(),
                next_allowed=False,
            ),
            "duplicate locator",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(completed_queries=("received-valid",)),
            dorset_adapter._ResultPage(
                references=(), page=1, total_pages=1, form=(), next_allowed=False
            ),
            "completed query",
        ),
    ],
)
def test_dorset_checkpoint_advance_rejects_contradictions(
    progress: dorset_adapter.DorsetCheckpointV1,
    page: Any,
    message: str,
) -> None:
    """A page cannot mutate a checkpoint that contradicts its durable history."""
    with pytest.raises(ValueError, match=message):
        dorset_adapter._advance_checkpoint(
            progress,
            query_key="received-valid",
            page=page,
            query_keys=("received-valid", "outstanding"),
        )


def test_dorset_checkpoint_replay_walks_every_committed_page() -> None:
    """Rehydration verifies each committed page before requesting the next one."""
    rows = tuple(
        _Result(f"P/REPLAY/2026/{index:05d}", 500_000 + index) for index in range(1, 22)
    )
    pages = {
        2: _three_page_result(2, rows[10:20]),
        3: _three_page_result(3, rows[20:]),
    }
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, content=pages[request_count + 1])

    session = _session(handler)
    first = dorset_adapter._parse_result_page(_three_page_result(1, rows[:10]))
    active = tuple(_live_reference(row) for row in rows[:20])
    progress = dorset_adapter.DorsetCheckpointV1(
        active_query="received-valid",
        next_page=3,
        total_pages=3,
        active_references=active,
    )

    async def replay() -> Any:
        result = await dorset_adapter._replay_active_query(session, progress, first)
        await session.aclose()
        return result

    result = asyncio.run(replay())
    assert result.page == 3
    assert result.references == (_live_reference(rows[20]),)
    assert request_count == 2


@pytest.mark.parametrize(
    ("progress", "page", "message"),
    [
        (
            dorset_adapter.DorsetCheckpointV1(
                active_query="received-valid", next_page=1
            ),
            dorset_adapter._parse_result_page(_result_page("received-valid", 1)),
            "active page",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(
                active_query="received-valid", next_page=2, total_pages=2
            ),
            dorset_adapter._parse_result_page(_result_page("received-valid", 1)),
            "active references",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(
                active_query="received-valid",
                next_page=2,
                total_pages=2,
                active_references=tuple(_live_reference(row) for row in RECEIVED[:10]),
            ),
            dorset_adapter._parse_result_page(_result_page("received-valid", 2)),
            "replayed page",
        ),
        (
            dorset_adapter.DorsetCheckpointV1(
                active_query="received-valid",
                next_page=2,
                total_pages=2,
                active_references=tuple(_live_reference(row) for row in RECEIVED[:10]),
            ),
            dorset_adapter._ResultPage(
                references=tuple(
                    _live_reference(row) for row in (*RECEIVED[:9], RECEIVED[10])
                ),
                page=1,
                total_pages=2,
                form=(),
                next_allowed=True,
            ),
            "replayed references",
        ),
    ],
)
def test_dorset_checkpoint_replay_rejects_changed_history(
    progress: dorset_adapter.DorsetCheckpointV1,
    page: Any,
    message: str,
) -> None:
    """Missing state or changed committed pages stop resume before advancement."""
    session = _session(_DorsetMock())

    async def reject() -> None:
        with pytest.raises(ValueError, match=message):
            await dorset_adapter._replay_active_query(session, progress, page)
        await session.aclose()

    asyncio.run(reject())


@pytest.mark.parametrize(
    ("body", "parser", "message"),
    [
        (
            _disclaimer_form().replace(b'method="post"', b'method="get"'),
            dorset_adapter._parse_disclaimer_form,
            "disclaimer form",
        ),
        (
            _advanced_form().replace(b'action="./advsearch.aspx"', b'action="/wrong"'),
            dorset_adapter._parse_advanced_form,
            "advanced form",
        ),
        (
            _advanced_form().replace(b'value="advanced-state"', b'value=""'),
            dorset_adapter._parse_advanced_form,
            "advanced form viewstate",
        ),
        (
            _result_page("received-valid", 1).replace(
                b'action="./searchresults.aspx"', b'action="/wrong"'
            ),
            dorset_adapter._parse_result_page,
            "result form",
        ),
    ],
)
def test_dorset_forms_fail_closed_on_identity_and_state_changes(
    body: bytes,
    parser: Any,
    message: str,
) -> None:
    """Changed methods, actions, and ASP.NET state cannot be silently submitted."""
    with pytest.raises(ValueError, match=message):
        parser(body)


def test_dorset_disclaimer_rejects_empty_published_state() -> None:
    """Consent is not submitted with an empty anti-forgery field."""
    body = _disclaimer_form().replace(b'value="758A299B"', b'value=""')
    with pytest.raises(ValueError, match="disclaimer form state"):
        dorset_adapter._parse_disclaimer_form(body)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("recno", "result recno"),
        ("reference", "result reference"),
        ("empty-terminal", "terminal result rows"),
        ("top-next", "next page"),
        ("bottom-next", "next page"),
    ],
)
def test_dorset_result_rows_fail_closed_on_malformed_identity_or_paging(
    fault: str,
    message: str,
) -> None:
    """Malformed result identity and pager controls never become references."""
    body = _result_page("received-valid", 2 if fault == "empty-terminal" else 1)

    def mutate(soup: BeautifulSoup) -> None:
        links = soup.select('a[id$="_hypDisplayRecord"]')
        if fault == "recno":
            links[0]["href"] = "plandisp.aspx?recno=bad"
        elif fault == "reference":
            links[0].clear()
        elif fault == "empty-terminal":
            for link in links:
                link.decompose()
        elif fault == "top-next":
            control = soup.select_one(f'input[name="{NEXT_BUTTON}"]')
            assert isinstance(control, Tag)
            control["value"] = "Next"
        else:
            control = soup.select_one(
                'input[name="ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton"]'
            )
            assert isinstance(control, Tag)
            control["value"] = "Next"

    with pytest.raises(ValueError, match=message):
        dorset_adapter._parse_result_page(_mutated(body, mutate))


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("reference", "detail reference"),
        ("authority", "detail authority"),
        ("section", "pvLocation"),
        ("missing-data", "detail label"),
        ("duplicate-label", "detail label"),
        ("missing-label", "detail label"),
        ("empty-value", "detail Status"),
        ("date", "detail date"),
        ("coordinate", "detail Easting"),
        ("table", "document grid"),
        ("row", "document grid"),
        ("href", "document grid"),
        ("onclick", "document grid"),
        ("rendered", "document grid"),
        ("indices", "document grid"),
    ],
)
def test_dorset_detail_fails_closed_on_malformed_metadata(
    fault: str,
    message: str,
) -> None:
    """Every required detail and document-grid invariant is fail closed."""
    reference = _live_reference()

    def mutate(soup: BeautifulSoup) -> None:
        labels = soup.select("#ctl00_ContentPlaceHolder1_pvDetails span.applabel")
        rows = soup.select("#ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00 tbody tr")
        if fault == "reference":
            data = labels[0].find_next_sibling("p")
            assert isinstance(data, Tag)
            data.string = "P/OTHER/1"
        elif fault == "authority":
            data = labels[-1].find_next_sibling("p")
            assert isinstance(data, Tag)
            data.string = "Another Council"
        elif fault == "section":
            section = soup.select_one("#ctl00_ContentPlaceHolder1_pvLocation")
            assert isinstance(section, Tag)
            section.decompose()
        elif fault == "missing-data":
            data = labels[-1].find_next_sibling("p")
            assert isinstance(data, Tag)
            data.decompose()
        elif fault == "duplicate-label":
            labels[1].string = labels[0].get_text()
        elif fault == "missing-label":
            labels[-1].decompose()
        elif fault == "empty-value":
            data = labels[1].find_next_sibling("p")
            assert isinstance(data, Tag)
            data.clear()
        elif fault == "date":
            data = labels[4].find_next_sibling("p")
            assert isinstance(data, Tag)
            data.string = "2026-09-15"
        elif fault == "coordinate":
            data = soup.find("span", string="Easting")
            assert isinstance(data, Tag)
            value = data.find_next_sibling("p")
            assert isinstance(value, Tag)
            value.string = "east"
        elif fault == "table":
            table = soup.select_one("#ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00")
            assert isinstance(table, Tag)
            table.decompose()
        elif fault == "row":
            rows[0]["id"] = "wrong"
        elif fault == "href":
            link = rows[0].select_one("a")
            assert isinstance(link, Tag)
            link["href"] = "/attachment.pdf"
        elif fault == "onclick":
            link = rows[0].select_one("a")
            assert isinstance(link, Tag)
            link["onclick"] = "return RowClicked(9);"
        elif fault == "rendered":
            link = rows[0].select_one("a")
            assert isinstance(link, Tag)
            link.string = "Application Form"
        else:
            rows[1]["id"] = "ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00__3"
            link = rows[1].select_one("a")
            assert isinstance(link, Tag)
            link["onclick"] = "return RowClicked(3);"

    with pytest.raises(ValueError, match=message):
        dorset_adapter._parse_live_detail(
            _mutated(_detail_page(reference.reference), mutate),
            reference,
            HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={reference.locator}"),
        )


@pytest.mark.parametrize(("label", "field"), [("Ward", "ward"), ("Parish", "parish")])
def test_dorset_detail_preserves_explicitly_blank_optional_location(
    label: str,
    field: str,
) -> None:
    """Present but blank optional location labels remain honest native absence."""
    reference = _live_reference()

    def clear_value(soup: BeautifulSoup) -> None:
        location_label = soup.find("span", string=label)
        assert isinstance(location_label, Tag)
        value = location_label.find_next_sibling("p")
        assert isinstance(value, Tag)
        value.clear()

    native = dorset_adapter._parse_live_detail(
        _mutated(_detail_page(reference.reference), clear_value),
        reference,
        HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={reference.locator}"),
    )

    assert getattr(native, field) is None


def _grid_script(payload: Any, *, direct: bool = False) -> str:
    encoded = json.dumps(payload, separators=(",", ":"))
    value = encoded if direct else json.dumps(encoded)
    return f'var grid={{"_gridTableViewsData":{value}}};'


def _detail_with_grid_script(script: str) -> BeautifulSoup:
    body = _detail_page(RECEIVED[0].reference).decode()
    start = body.index("<script>")
    end = body.index("</script>", start) + len("</script>")
    return BeautifulSoup(
        f"{body[:start]}<script>{script}</script>{body[end:]}", "html.parser"
    )


def test_dorset_document_grid_accepts_exact_direct_telerik_proof() -> None:
    """The non-escaped Telerik proof variant must carry the same exact counts."""
    soup = _detail_with_grid_script(
        _grid_script(
            [{"PageCount": 1, "AllowPaging": False, "VirtualItemCount": 2}],
            direct=True,
        )
    )
    documents = dorset_adapter._parse_documents_grid(
        soup,
        HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}"),
    )
    assert len(documents) == 2


def test_dorset_detail_accepts_exact_no_documents_sentinel() -> None:
    """The official Telerik zero-row sentinel proves an empty document section."""
    reference = _live_reference()

    native = dorset_adapter._parse_live_detail(
        _detail_page(reference.reference, document_count=0),
        reference,
        HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={reference.locator}"),
    )

    assert native.documents == ()


def test_dorset_detail_rejects_changed_no_documents_sentinel() -> None:
    """An unrecognised empty-grid message cannot stand in for completeness proof."""
    reference = _live_reference()
    body = _detail_page(reference.reference, document_count=0).replace(
        b"There are currently no scanned documents for this application.",
        b"Documents are unavailable.",
    )

    with pytest.raises(ValueError, match="document grid"):
        dorset_adapter._parse_live_detail(
            body,
            reference,
            HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={reference.locator}"),
        )


@pytest.mark.parametrize(
    "payload",
    [
        "malformed",
        [],
        [1],
        [{"PageCount": 1, "AllowPaging": "false", "VirtualItemCount": 2}],
    ],
)
def test_dorset_document_grid_rejects_invalid_telerik_proof(payload: Any) -> None:
    """Malformed, ambiguous, and mistyped grid metadata cannot prove completeness."""
    soup = _detail_with_grid_script(
        'var grid={"_gridTableViewsData":"not-json"};'
        if payload == "malformed"
        else _grid_script(payload)
    )
    with pytest.raises(ValueError, match="document grid"):
        dorset_adapter._parse_documents_grid(
            soup,
            HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}"),
        )


def test_dorset_document_grid_ignores_unrelated_script_before_exact_proof() -> None:
    """Unrelated page scripts do not substitute for the required Telerik proof."""
    soup = BeautifulSoup(_detail_page(RECEIVED[0].reference), "html.parser")
    unrelated = soup.new_tag("script")
    unrelated.string = "window.unrelated = true;"
    soup.insert(0, unrelated)
    documents = dorset_adapter._parse_documents_grid(
        soup,
        HttpUrl(f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}"),
    )
    assert len(documents) == 2


def test_dorset_successful_controls_follow_browser_submission_rules() -> None:
    """Only successful controls are retained, including ordered select values."""
    form = _form(
        b"""
        <form>
          <input name="disabled" value="no" disabled>
          <input type="checkbox" name="checked" checked>
          <input type="radio" name="included" value="yes">
          <select name="fallback"><option value="first">First</option></select>
          <select name="empty"></select>
          <select name="single">
            <option value="one" selected>One</option>
            <option value="two" selected>Two</option>
          </select>
          <select name="many" multiple>
            <option value="one" selected>One</option>
            <option value="two" selected>Two</option>
          </select>
          <textarea name="notes"> hello </textarea>
        </form>
        """
    )
    assert dorset_adapter._successful_controls(
        form,
        frozenset({"included"}),
    ) == (
        FormField(name="checked", value="on"),
        FormField(name="included", value="yes"),
        FormField(name="fallback", value="first"),
        FormField(name="single", value="one"),
        FormField(name="many", value="one"),
        FormField(name="many", value="two"),
        FormField(name="notes", value=" hello "),
    )


def test_dorset_form_helpers_reject_missing_state_submit_and_action() -> None:
    """The local form helpers fail closed when exact state or action is absent."""
    form = _form(b'<form action="/wrong"><input name="present"></form>')
    with pytest.raises(ValueError, match="form state"):
        dorset_adapter._require_fields((), "missing")
    with pytest.raises(ValueError, match="form state"):
        dorset_adapter._require_hidden_inputs(form, "present")
    with pytest.raises(ValueError, match="form submit"):
        dorset_adapter._submit_value(form, "submit", "Search")
    with pytest.raises(ValueError, match="disclaimer form"):
        dorset_adapter._accept_disclaimer_request(form)
    with pytest.raises(ValueError, match="one form"):
        dorset_adapter._single_form(b"<p>none</p>", "one form")


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("marker-count", "page markers"),
        ("marker-text", "page markers"),
        ("current-text", "current page"),
        ("current-mismatch", "page markers"),
        ("pager-mismatch", "page markers"),
    ],
)
def test_dorset_page_markers_reject_ambiguous_paging(fault: str, message: str) -> None:
    """Both pagers must independently state the same valid current page."""
    form = _form(_result_page("received-valid", 1))
    pagers = form.select(
        "#ctl00_ContentPlaceHolder1_lvResults_RadDataPager1, "
        "#ctl00_ContentPlaceHolder1_lvResults_pager"
    )
    if fault == "marker-count":
        pagers[-1].decompose()
    elif fault == "marker-text":
        span = pagers[0].select_one("span")
        assert isinstance(span, Tag)
        span.string = "Page unknown"
    elif fault == "current-text":
        pagers[0].select_one(".rdpCurrentPage").string = "zero"  # type: ignore[union-attr]
    elif fault == "current-mismatch":
        pagers[0].select_one(".rdpCurrentPage").string = "2"  # type: ignore[union-attr]
    else:
        pagers[-1].select_one(".rdpCurrentPage").string = "2"  # type: ignore[union-attr]
        span = pagers[-1].select_one("span")
        assert isinstance(span, Tag)
        span.string = "Page 2 of 2"
    with pytest.raises(ValueError, match=message):
        dorset_adapter._page_markers(form)


def test_dorset_page_and_required_helpers_reject_impossible_requests() -> None:
    """No next request or regex value is invented when its proof is absent."""
    terminal = dorset_adapter._parse_result_page(_result_page("received-valid", 2))
    with pytest.raises(ValueError, match="next page"):
        dorset_adapter._next_page_request(terminal)
    with pytest.raises(ValueError, match="field value"):
        dorset_adapter._required("body", r"value=(\d+)", "value")
