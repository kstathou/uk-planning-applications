# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, E501, EM101, PLR0911, PLR0915, PLR2004, SLF001, TRY003

"""Real HTTP contract boundaries for Arun, Devon, Camden, and Peak District."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from pydantic import HttpUrl

import yimby.authorities.arun.adapter as arun
import yimby.authorities.camden.adapter as camden
import yimby.authorities.camden.discovery as camden_discovery
import yimby.authorities.devon.adapter as devon
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
from yimby.transport import (
    PortalRequest,
    RequestIntent,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from types import ModuleType

_SEARCH_DIGEST = EvidenceDigest("0" * 64)


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


def _devon_qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_devon.py"
    name = "_test_qualify_devon"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _arun_form() -> bytes:
    return b"""
    <form action="planningSearch" method="post" name="OcellaPlanningSearch">
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
        f'<tr><td><a href="planningDetails?reference={reference.replace("/", "%2F")}&amp;from=planningSearch">{reference}</a></td>'
        "<td>Site</td><td>Proposal</td><td>Undecided</td></tr>"
        for reference in references
    )
    controls = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in fields.items()
        if name != "action"
    )
    show_all_control = (
        '<form method="post" action="planningSearch">'
        '<input type="hidden" name="action" value="Search">'
        '<input type="hidden" name="showall" value="showall">'
        f'{controls}<input type="submit" value="Show all results"></form>'
        if show_all
        else ""
    )
    exact_control = (
        '<form method="post" name="search" action="planningSearch">'
        '<input type="submit" name="BackToSearch" value="Back to Search page">'
        "</form>"
        if not show_all and reported == len(references)
        else ""
    )
    count = (
        f"<strong>First {len(references)} results shown, "
        f"there are {reported} in total</strong>"
        if show_all or reported != len(references)
        else ""
    )
    return (
        f"{count}{exact_control}<table><tr><th>Reference</th><th>Location</th>"
        f"<th>Proposal</th><th>Status</th></tr>{rows}</table>{show_all_control}"
    ).encode()


def _arun_empty_results() -> bytes:
    return _arun_form().replace(
        b"</form>",
        b'<strong><span style="color:maroon">'
        b"No applications found for entered search criteria"
        b"</span></strong></form>",
        1,
    )


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
            return _arun_empty_results()
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


def _devon_advanced_form() -> bytes:
    return b"""
    <form id="advancedSearchForm" action="/Search/Results" method="post">
      <input type="hidden" name="__RequestVerificationToken" value="sanitised">
      <input type="hidden" name="AdvancedSearch" value="true">
      <input type="checkbox" name="Outstanding" value="true">
      <input type="hidden" name="Outstanding" value="false">
      <input type="checkbox" name="SearchPlanning" value="true" checked>
      <input type="hidden" name="SearchPlanning" value="false">
      <input type="checkbox" name="SearchEnforcement" value="true">
      <input type="hidden" name="SearchEnforcement" value="false">
      <input type="checkbox" name="SearchAppeals" value="true">
      <input type="hidden" name="SearchAppeals" value="false">
      <input name="ApplicationOrDistrictNumbers" value="old">
      <input name="Address" value="old">
      <input name="Proposal" value="old">
      <select name="Parish"><option value="" selected>Any</option></select>
      <select name="Ward"><option value="" selected>Any</option></select>
      <select name="District"><option value="" selected>Any</option></select>
      <select name="Radius"><option value="" selected>Any</option></select>
      <select name="Decision"><option value="" selected>Any</option></select>
      <input name="DateReceivedFrom" value="old">
      <input name="DateReceivedTo" value="old">
      <input name="DateDeterminedFrom" value="old">
      <input name="DateDeterminedTo" value="old">
      <select name="ApplicationType"><option value="" selected>Any</option></select>
      <select name="AppealMethod"><option value="" selected>Any</option></select>
      <select name="AppealDecision"><option value="" selected>Any</option></select>
      <input name="PinsRef" value="old">
      <input name="DateAppealFrom" value="old">
      <input name="DateAppealTo" value="old">
      <input name="DateAppealDecisionFrom" value="old">
      <input name="DateAppealDecisionTo" value="old">
      <button type="submit" name="submit" value="Search">Search</button>
    </form>
    """


def _devon_results(  # noqa: PLR0913
    references: tuple[str, ...],
    *,
    route: str = "Planning",
    page: int = 1,
    total_pages: int = 1,
    current_markers: int = 1,
    forward: bool | None = None,
) -> bytes:
    records = "".join(
        f"""
        <dl class="searchResultsList">
          <dt>Application number</dt>
          <dd><a href="/{route}/Display/{reference}">{reference}</a></dd>
          <dt>Proposal</dt><dd>Upgrade recycling centre</dd>
        </dl>
        """
        for reference in references
    )
    if total_pages == 1:
        return records.encode()
    numbered: list[str] = []
    for number in range(1, total_pages + 1):
        href = "/Search/Results" if number == 1 else f"/Search/Results/{number}"
        if number == page and current_markers:
            numbered.extend(
                f'<li class="active"><span>{number}</span></li>'
                for _ in range(current_markers)
            )
        else:
            numbered.append(f'<li><a href="{href}">{number}</a></li>')
    has_forward = page < total_pages if forward is None else forward
    next_item = (
        f'<li><a rel="next" href="/Search/Results/{page + 1}">Next</a></li>'
        if has_forward
        else '<li class="disabled"><span>Next</span></li>'
    )
    pager = f'<ul class="pagination">{"".join(numbered)}{next_item}</ul>'
    return f"{records}{pager}".encode()


def _devon_detail(reference: str = "DCC/4473/2026") -> bytes:
    return f"""
    <dl class="details-grid">
      <dt>Application Number</dt><dd>{reference}</dd>
      <dt>Application Type</dt><dd>County Development</dd>
      <dt>Proposal</dt><dd>Upgrade recycling centre</dd>
      <dt>Status</dt><dd>Under Consideration</dd>
      <dt>Location</dt><dd>North Devon recycling centre</dd>
      <dt>Case Officer</dt><dd>Officer Two</dd>
      <dt>Date Received</dt><dd>20/08/2026</dd>
      <dt>Date Valid</dt><dd>21 August 2026</dd>
      <dt>Consultation Expiry</dt><dd>08/10/2026</dd>
      <dt>Decision Level</dt><dd>Delegated</dd>
      <dt>Decision Date</dt><dd>-</dd>
      <dt>Committee Date</dt><dd>01/10/2026</dd>
      <dt>Decision</dt><dd>Awaiting decision</dd>
      <dt>Issue Date</dt><dd>-</dd>
      <dt>Applicant's Address</dt><dd>Applicant House, Devon</dd>
      <dt>Agent's Address</dt><dd>Agent House, Devon</dd>
    </dl>
    <dl class="details-grid">
      <dt>District(s)</dt><dd>North Devon</dd>
      <dt>Electoral Division(s)</dt><dd>Braunton Rural</dd>
      <dt>Parish(es)</dt><dd>Georgeham</dd>
      <dt>Applicant</dt><dd>Applicant Two</dd>
      <dt>Agent</dt><dd>Agent Two</dd>
      <dt>Local Member(s)</dt><dd>Member One\nMember Two</dd>
    </dl>
    <script>var easting = 300476; var northing = 91039;</script>
    <div id="PlanningdocTable" aria-label="Document grid"></div>
    <table class="tblTest table sortable document-list">
      <thead><tr><th>All</th><th>Description <span class="sorted">&#9660;</span></th><th>Created date <span class="sorted"></span></th></tr></thead>
      <tbody>
        <tr class="header active"><th colspan="3">PLANS &amp; DRAWINGS</th></tr>
        <tr><td><input type="checkbox"></td><td>
          <a href="/Document/Download?module=pl&amp;recordNumber=4473&amp;planId=1&amp;imageId=2&amp;isPlan=true&amp;fileName=site-plan.pdf">Site plan</a>
        </td><td>20/08/2026</td></tr>
      </tbody>
      <tbody>
        <tr class="header active"><th colspan="3">CONSULTATION RESPONSES</th></tr>
        <tr><td><input type="checkbox"></td><td>
          <a href="/Document/Download?module=pl&amp;recordNumber=4473&amp;planId=3&amp;imageId=4&amp;isPlan=false">Consultation response</a>
        </td><td>22/08/2026</td></tr>
      </tbody>
    </table>
    <table summary="Planning Constraints"><thead><tr><th>Description</th></tr></thead>
      <tbody><tr><td>Mineral safeguarding area</td></tr></tbody>
    </table>
    <table summary="Planning Consultees"><thead><tr>
      <th>Consultee Name</th><th>Date Letter Sent</th>
      <th>Consultation Expiry Date</th><th>Reply Received</th>
    </tr></thead><tbody><tr>
      <td>Environment Agency</td><td>20/08/2026</td><td>08/10/2026</td><td>-</td>
    </tr></tbody></table>
    """.encode()


def _devon_appeal_detail(reference: str = "APP/J1155/W/22/3299799") -> bytes:
    return f"""
    <dl class="details-grid">
      <dt>Planning Ref</dt><dd>DCC/3945/2017</dd>
      <dt>Enforcement Ref</dt><dd>ENF/0997/2019</dd>
      <dt>Location</dt><dd>Straitgate Farm, Exeter Road</dd>
      <dt>UPRN</dt><dd>-</dd>
      <dt>Site</dt><dd>MD/500901/M</dd>
      <dt>Proposal</dt><dd>Minerals appeal</dd>
      <dt>Type</dt><dd>s78 Appeal</dd>
      <dt>Appeal Method</dt><dd>Inquiry</dd>
      <dt>Appellant</dt><dd>-</dd>
      <dt>Agent</dt><dd>-</dd>
      <dt>Appellant Address</dt><dd>Appellant House, Devon</dd>
      <dt>Agents Address</dt><dd>Agent House, Devon</dd>
    </dl>
    <dl class="details-grid">
      <dt>Start Date</dt><dd>21/06/2022</dd>
      <dt>Site Visit</dt><dd>-</dd>
      <dt>Questionnaire Sent</dt><dd>-</dd>
      <dt>Questionnaire Due</dt><dd>28/06/2022</dd>
      <dt>Statement Sent</dt><dd>-</dd>
      <dt>Statement Due</dt><dd>26/07/2022</dd>
      <dt>Proof of Evidence Sent</dt><dd>-</dd>
      <dt>Proof of Evidence Due</dt><dd>06/09/2022</dd>
      <dt>Inquiry Date</dt><dd>04/10/2022</dd>
      <dt>Appeal Officer</dt><dd>Officer Three</dd>
      <dt>Venue</dt><dd>-</dd>
      <dt>Available From</dt><dd>-</dd>
      <dt>Available To</dt><dd>-</dd>
      <dt>PINS Ref</dt><dd>3299799</dd>
      <dt>PINS Officer</dt><dd>-</dd>
    </dl>
    <dl class="details-grid">
      <dt>Parish</dt><dd>Ottery St Mary</dd>
      <dt>Ward</dt><dd>West Hill &amp; Aylesbeare</dd>
      <dt>Inspector</dt><dd>-</dd>
      <dt>Planning Officer</dt><dd>Officer Four</dd>
      <dt>Easting</dt><dd>307500</dd>
      <dt>Northing</dt><dd>97200</dd>
    </dl>
    <dl class="details-grid">
      <dt>Decision Date</dt><dd>-</dd>
      <dt>In Abeyance</dt><dd>-</dd>
      <dt>Abeyance Date</dt><dd>-</dd>
      <dt>Appeal Decision</dt><dd>Withdrawn</dd>
      <dt>Decision</dt><dd>Applicant</dd>
    </dl>
    <dl class="details-grid">
      <dt>Council Applied</dt><dd>-</dd>
      <dt>Council Awarded</dt><dd>-</dd>
      <dt>Appellant Applied</dt><dd>-</dd>
      <dt>Appellant Awarded</dt><dd>-</dd>
    </dl>
    <div id="PlanningdocTable" aria-label="Document grid"></div>
    <table class="tblTest table sortable document-list">
      <thead><tr><th>All</th><th>Description <span>▼</span></th><th>Created date <span></span></th></tr></thead>
      <tbody>
        <tr class="header active"><th colspan="3">APPEAL DOCUMENTS</th></tr>
        <tr><td><input type="checkbox"></td><td>
          <a href="/Document/Download?module=APP&amp;recordNumber=44&amp;planId=0&amp;imageId=2&amp;isPlan=false&amp;fileName=start-letter.pdf">Start letter</a>
        </td><td>21/06/2022</td></tr>
      </tbody>
    </table>
    <table summary="Appeal Consultees"><thead><tr>
      <td>Consultee Name</td><td>Date Letter Sent</td>
      <td>Consultation Expiry Date</td><td>Reply Received</td>
    </tr></thead><tbody><tr>
      <td>West Hill Parish Council</td><td>-</td><td>-</td><td>-</td>
    </tr></tbody></table>
    <p data-appeal-reference="{reference}"></p>
    """.encode()


class _DevonMock:
    def __init__(
        self,
        *,
        direct: bool = False,
        repeated_disclaimer: bool = False,
        malformed_page: int | None = None,
        shift_first_open: bool = False,
        mismatch_detail: bool = False,
    ) -> None:
        self.direct = direct
        self.repeated_disclaimer = repeated_disclaimer
        self.malformed_page = malformed_page
        self.shift_first_open = shift_first_open
        self.mismatch_detail = mismatch_detail
        self.pending: tuple[str, int] | None = None
        self.detail_reference: str | None = None
        self.query_keys: list[str] = []

    def _result(self) -> bytes:
        assert self.pending is not None
        kind, page = self.pending
        if kind == "received":
            return _devon_results(("DCC/4473/2026", "DCC/4472/2026", "PRE/1820/2026"))
        if kind == "determined":
            return _devon_detail("PRE/1820/2026")
        if kind == "appeal-received":
            return b"<p>No records</p>"
        if kind == "appeal-determined":
            return b"<p>No records</p>"
        if kind == "outstanding-appeals":
            return _devon_results(("APP/J1155/W/22/3299799",), route="Appeals")
        start = (page - 1) * 10
        if page == 1 and self.shift_first_open:
            start += 1
        references = tuple(
            f"OPEN/{number:03d}/2026"
            for number in range(start + 1, min(start + 11, 56))
        )
        if self.malformed_page == page:
            return _devon_results(
                references, page=page, total_pages=6, current_markers=0
            )
        return _devon_results(references, page=page, total_pages=6)

    def _select_query(self, request: PortalRequest) -> None:
        values: dict[str, list[str]] = {}
        for field in request.form:
            values.setdefault(field.name, []).append(field.value)

        def iso_date(value: str) -> str:
            day, month, year = value.split("/")
            return f"{year}-{month}-{day}"

        appeals = "true" in values.get("SearchAppeals", [])
        outstanding = "true" in values.get("Outstanding", [])
        if outstanding and appeals:
            key = "outstanding:appeals:true"
            kind = "outstanding-appeals"
        elif outstanding:
            key = "outstanding:planning:true"
            kind = "outstanding"
        elif values.get("DateReceivedFrom", [""])[0]:
            start = iso_date(values["DateReceivedFrom"][0])
            end = iso_date(values["DateReceivedTo"][0])
            key = f"received:{start}:{end}"
            kind = "received"
        elif values.get("DateDeterminedFrom", [""])[0]:
            start = iso_date(values["DateDeterminedFrom"][0])
            end = iso_date(values["DateDeterminedTo"][0])
            key = f"determined:{start}:{end}"
            kind = "determined"
        elif values.get("DateAppealFrom", [""])[0]:
            start = iso_date(values["DateAppealFrom"][0])
            end = iso_date(values["DateAppealTo"][0])
            key = f"appeal-received:{start}:{end}"
            kind = "appeal-received"
        else:
            start = iso_date(values["DateAppealDecisionFrom"][0])
            end = iso_date(values["DateAppealDecisionTo"][0])
            key = f"appeal-determined:{start}:{end}"
            kind = "appeal-determined"
        self.query_keys.append(key)
        self.pending = kind, 1

    def __call__(self, request: PortalRequest) -> bytes:  # noqa: C901
        url = str(request.url)
        if request.method == RequestMethod.POST and "/Disclaimer/Accept" in url:
            if self.repeated_disclaimer:
                return _devon_disclaimer("again")
            if "advanced" in url.casefold():
                return _devon_advanced_form()
            if "results" in url.casefold():
                return self._result()
            if "/appeals/display/" in url.casefold():
                return _devon_appeal_detail(cast("str", self.detail_reference))
            return _devon_detail(
                "WRONG/1"
                if self.mismatch_detail
                else cast("str", self.detail_reference)
            )
        if url.rstrip("/") == devon._ADVANCED_FORM_URL:
            return (
                _devon_advanced_form()
                if self.direct
                else _devon_disclaimer("/Search/Advanced")
            )
        if (
            url.rstrip("/") == devon._RESULTS_URL
            and request.method == RequestMethod.POST
        ):
            self._select_query(request)
            return (
                self._result() if self.direct else _devon_disclaimer("/Search/Results")
            )
        if "/Search/Results/" in url:
            assert self.pending is not None
            assert self.pending[0] == "outstanding"
            self.pending = "outstanding", int(urlsplit(url).path.rsplit("/", 1)[-1])
            return (
                self._result() if self.direct else _devon_disclaimer(urlsplit(url).path)
            )
        if "/Planning/Display/" in url:
            reference = urlsplit(url).path.partition("/Planning/Display/")[2]
            self.detail_reference = reference
            if self.direct:
                return _devon_detail("WRONG/1" if self.mismatch_detail else reference)
            return _devon_disclaimer(urlsplit(url).path)
        if "/Appeals/Display/" in url:
            reference = urlsplit(url).path.partition("/Appeals/Display/")[2]
            self.detail_reference = reference
            if self.direct:
                return _devon_appeal_detail(reference)
            return _devon_disclaimer(urlsplit(url).path)
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


def _camden_detail(
    reference: str = "2026/2706/L",
    *,
    coordinates: bool = True,
    proposal: str = "Repair listed townhouse",
) -> bytes:
    coordinate_rows = (
        "<tr><th>Easting</th><td>530748</td></tr>"
        "<tr><th>Northing</th><td>182755</td></tr>"
        if coordinates
        else ""
    )
    return f"""
    <div class="dataview"><table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Address</th><td>1 Camden Square</td></tr>
      <tr><th>Application Type</th><td>Listed Building Consent</td></tr>
      <tr><th>Development Type</th><td>Alterations</td></tr>
      <tr><th>Proposal</th><td>{proposal}</td></tr>
      <tr><th>Current Status</th><td>Decision Issued</td></tr>
      <tr><th>Applicant</th><td>Applicant Three</td></tr>
      <tr><th>Agent</th><td>Agent Three</td></tr>
      <tr><th>Ward</th><td>Camden Square</td></tr>
      {coordinate_rows}
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
        missing_coordinates: bool = False,
        empty_fields: bool = False,
        document_failure: bool = False,
        document_reported: int = 2,
    ) -> None:
        self.mismatch_detail = mismatch_detail
        self.missing_coordinates = missing_coordinates
        self.empty_fields = empty_fields
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
            return _camden_detail(
                "WRONG/1" if self.mismatch_detail else "2026/2706/L",
                coordinates=not self.missing_coordinates,
                proposal="" if self.empty_fields else "Repair listed townhouse",
            )
        if url.startswith(camden.DOCUMENT_BASE):
            query = parse_qs(urlsplit(url).query)["q"]
            assert query == ['recContainer:"2026/2706/L"']
            if self.document_failure:
                raise SourceUnavailableError("document service unavailable")
            if self.empty_fields:
                return b"There are no public documents for this application"
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
    discovery = store.discovery_state(AuthorityId("arun"))
    assert discovery.queued[0].locator is not None
    assert discovery.checkpoint is not None
    checkpoint = arun.ArunCheckpointV1.model_validate_json(
        discovery.checkpoint.payload_json
    )
    assert isinstance(checkpoint.cursor, arun.ArunLiveCursor)
    assert checkpoint.cursor.search_form_evidence is not None
    form_evidence = store.evidence_capture(checkpoint.cursor.search_form_evidence)
    assert form_evidence is not None
    assert form_evidence.body == _arun_form()
    assert store.evidence_capture(EvidenceDigest("f" * 64)) is None
    assert isinstance(checkpoint.cursor.progress, arun.ArunComplete)
    for completed in checkpoint.cursor.progress.completed:
        assert store.evidence_capture(completed.initial_evidence) is not None
        if completed.expanded_evidence is not None:
            assert store.evidence_capture(completed.expanded_evidence) is not None
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
    adapter = devon.DevonAdapter()
    package = AuthorityPackage(
        adapter, devon.DevonApplicationV1, devon.DevonCheckpointV1
    )
    store = _store(tmp_path)
    collector = Collector(_registry(package), store)
    window = DiscoveryWindow(
        start=date(2026, 8, 18), end=date(2026, 9, 16), include_open=False
    )
    responder = _DevonMock()
    session = _Session(responder)
    report = asyncio.run(collector.collect(AuthorityId("devon"), window, session))
    assert len(report.applications) == 3
    assert responder.query_keys == [
        "received:2026-08-18:2026-09-16",
        "determined:2026-08-18:2026-09-16",
        "appeal-received:2026-08-18:2026-09-16",
        "appeal-determined:2026-08-18:2026-09-16",
    ]
    stored = store.get_application(report.applications[0])
    assert sorted(item.title for item in stored.documents) == [
        "Consultation response",
        "Site plan",
    ]
    assert stored.completeness.comments.kind == "unavailable"
    assert {item.category for item in stored.documents} == {
        "CONSULTATION RESPONSES",
        "PLANS & DRAWINGS",
    }
    assert {item.published_date for item in stored.documents} == {
        date(2026, 8, 20),
        date(2026, 8, 22),
    }
    assert report.attachment_body_requests == 0
    assert all("Document/Download" not in url for url in report.requested_urls)
    assert (
        sum(request.method == RequestMethod.POST for request in session.requests) == 15
    )
    view = store.application_view(report.applications[0])
    assert view.metadata.address == "North Devon recycling centre"
    assert view.metadata.validated_date == date(2026, 8, 21)
    assert view.metadata.decision_date is None
    assert view.metadata.location is not None
    assert view.metadata.location.bng_easting == 300476
    assert view.metadata.location.bng_northing == 91039
    assert view.metadata.constraints == ("Mineral safeguarding area",)
    assert view.metadata.consultations == (
        "Environment Agency | 20/08/2026 | 08/10/2026 | -",
    )
    assert {event.event_type for event in view.metadata.events} == {
        "committee",
        "consultation-expiry",
    }
    native = next(
        devon.DevonApplicationV1.model_validate_json(item.native_json)
        for item in store.retained_native_records()
        if item.reference.reference == "DCC/4473/2026"
    )
    assert native.local_members == ("Member One", "Member Two")
    with closing(sqlite3.connect(tmp_path / "yimby.sqlite3")) as connection:
        metadata = tuple(
            connection.execute(
                "SELECT category, published_date FROM document_metadata "
                "WHERE application_id = ? ORDER BY category",
                (report.applications[0],),
            )
        )
    assert metadata == (
        ("CONSULTATION RESPONSES", "2026-08-22"),
        ("PLANS & DRAWINGS", "2026-08-20"),
    )
    store.close()


def test_devon_collects_exact_appeal_route_and_native_fields() -> None:
    adapter = devon.DevonAdapter()
    reference = SourceReference(
        source_id=devon.APPEAL_SOURCE,
        reference="APP/J1155/W/22/3299799",
        locator=(f"{devon.BASE_URL}/Appeals/Display/APP/J1155/W/22/3299799"),
    )
    snapshot = asyncio.run(adapter.fetch(_Session(_DevonMock(direct=True)), reference))
    assert snapshot.payload.council_reference == reference.reference
    assert snapshot.payload.record_kind == "appeal"
    assert snapshot.payload.related_planning_reference == "DCC/3945/2017"
    assert snapshot.payload.appeal_method == "Inquiry"
    assert snapshot.payload.pins_reference == "3299799"
    assert snapshot.payload.consultations == (
        devon.DevonConsultationV1(values=("West Hill Parish Council", "-", "-", "-")),
    )
    assert snapshot.payload.documents[0].category == "APPEAL DOCUMENTS"
    normalised = adapter.normalise(snapshot)
    assert normalised.status == "withdrawn"
    assert normalised.metadata.application_type == "s78 Appeal"
    assert normalised.metadata.decision == "Withdrawn"
    assert normalised.metadata.aliases == ("3299799",)
    assert normalised.metadata.published_parties == ()
    placeholder_decision = adapter.normalise(
        snapshot.model_copy(
            update={
                "payload": snapshot.payload.model_copy(update={"appeal_decision": "-"})
            }
        )
    )
    assert placeholder_decision.metadata.decision is None
    assert {
        (item.relationship_type, item.related_reference)
        for item in normalised.metadata.relationships
    } == {
        ("appeal-of-planning", "DCC/3945/2017"),
        ("appeal-of-enforcement", "ENF/0997/2019"),
    }
    assert normalised.metadata.received_date == date(2022, 6, 21)
    assert normalised.metadata.location is not None
    assert normalised.metadata.location.bng_easting == 307500
    assert {event.event_type for event in normalised.metadata.events} >= {
        "appeal-start",
        "questionnaire-due",
        "statement-due",
        "proof-of-evidence-due",
        "inquiry",
    }

    class _RedirectedAppealSession(_Session):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            capture = await super().fetch(request)
            return capture.model_copy(
                update={
                    "url": HttpUrl(
                        f"{devon.BASE_URL}/Appeals/Display/DIFFERENT/REFERENCE"
                    )
                }
            )

    with pytest.raises(devon.DevonReferenceMismatchError):
        asyncio.run(
            adapter.fetch(_RedirectedAppealSession(_DevonMock(direct=True)), reference)
        )


def test_devon_exact_query_inventory_pagination_resume_and_replay() -> None:
    adapter = devon.DevonAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18), end=date(2026, 9, 16), include_open=True
    )
    responder = _DevonMock()
    session = _Session(responder)

    async def partial() -> Any:
        batches = cast("AsyncGenerator[Any]", adapter.discover(session, window, None))
        received = await anext(batches)
        determined = await anext(batches)
        open_first = await anext(batches)
        await batches.aclose()
        return received, determined, open_first

    received, determined, open_first = asyncio.run(partial())
    assert [item.reference for item in received.references] == [
        "DCC/4473/2026",
        "DCC/4472/2026",
        "PRE/1820/2026",
    ]
    assert determined.references == ()
    assert [item.reference for item in open_first.references] == [
        f"OPEN/{number:03d}/2026" for number in range(1, 11)
    ]
    assert responder.query_keys == [
        "received:2026-08-18:2026-09-16",
        "determined:2026-08-18:2026-09-16",
        "outstanding:planning:true",
    ]
    submitted = [
        request
        for request in session.requests
        if str(request.url).rstrip("/") == devon._RESULTS_URL
        and request.method == RequestMethod.POST
    ]
    assert len(submitted) == 3
    submitted_values = []
    for request in submitted:
        values: dict[str, list[str]] = {}
        for field in request.form:
            values.setdefault(field.name, []).append(field.value)
        submitted_values.append(values)
        assert values["__RequestVerificationToken"] == ["sanitised"]
        assert values["SearchPlanning"] == ["true", "false"]
        assert values["SearchEnforcement"] == ["false"]
        assert values["SearchAppeals"] == ["false"]
        assert values["ApplicationOrDistrictNumbers"] == [""]
        assert values["Proposal"] == [""]
    assert submitted_values[0]["DateReceivedFrom"] == ["18/08/2026"]
    assert submitted_values[0]["DateReceivedTo"] == ["16/09/2026"]
    assert submitted_values[0]["Outstanding"] == ["false"]
    assert submitted_values[1]["DateDeterminedFrom"] == ["18/08/2026"]
    assert submitted_values[1]["DateDeterminedTo"] == ["16/09/2026"]
    assert submitted_values[1]["Outstanding"] == ["false"]
    assert submitted_values[2]["Outstanding"] == ["true", "false"]

    checkpoint = open_first.next_checkpoint
    resumed_responder = _DevonMock()
    resumed_session = _Session(resumed_responder)
    resumed = asyncio.run(_batches(adapter, resumed_session, window, checkpoint))
    assert [len(batch.references) for batch in resumed] == [10, 10, 10, 10, 5, 0, 0, 1]
    assert resumed[-1].complete
    assert resumed_responder.query_keys == [
        "outstanding:planning:true",
        "appeal-received:2026-08-18:2026-09-16",
        "appeal-determined:2026-08-18:2026-09-16",
        "outstanding:appeals:true",
    ]
    assert tuple(
        urlsplit(str(request.url)).path
        for request in resumed_session.requests
        if request.method == RequestMethod.GET
        and "/Search/Results/" in str(request.url)
    ) == tuple(f"/Search/Results/{page}" for page in range(2, 7))
    final = resumed[-1].next_checkpoint
    audit = devon.qualification_audit(final, window)
    assert audit.terminal_coherent
    assert (
        audit.expected_queries
        == audit.completed_queries
        == (
            "received:2026-08-18:2026-09-16",
            "determined:2026-08-18:2026-09-16",
            "outstanding:planning:true",
            "appeal-received:2026-08-18:2026-09-16",
            "appeal-determined:2026-08-18:2026-09-16",
            "outstanding:appeals:true",
        )
    )
    assert audit.query_summaries == (
        devon.DevonQuerySummaryV1(
            query_key="received:2026-08-18:2026-09-16",
            row_count=3,
            page_count=1,
        ),
        devon.DevonQuerySummaryV1(
            query_key="determined:2026-08-18:2026-09-16",
            row_count=1,
            page_count=1,
        ),
        devon.DevonQuerySummaryV1(
            query_key="outstanding:planning:true",
            row_count=55,
            page_count=6,
        ),
        devon.DevonQuerySummaryV1(
            query_key="appeal-received:2026-08-18:2026-09-16",
            row_count=0,
            page_count=1,
        ),
        devon.DevonQuerySummaryV1(
            query_key="appeal-determined:2026-08-18:2026-09-16",
            row_count=0,
            page_count=1,
        ),
        devon.DevonQuerySummaryV1(
            query_key="outstanding:appeals:true",
            row_count=1,
            page_count=1,
        ),
    )
    assert len(audit.references) == 59

    next_window = DiscoveryWindow(
        start=date(2026, 8, 25), end=date(2026, 9, 23), include_open=True
    )
    next_responder = _DevonMock()
    next_cycle = asyncio.run(
        _batches(adapter, _Session(next_responder), next_window, final)
    )
    assert next_cycle[-1].complete
    assert next_cycle[-1].next_checkpoint.live_scope == devon.DevonDiscoveryScope(
        start=next_window.start,
        end=next_window.end,
        include_open=True,
    )
    assert next_responder.query_keys == [
        "received:2026-08-25:2026-09-23",
        "determined:2026-08-25:2026-09-23",
        "outstanding:planning:true",
        "appeal-received:2026-08-25:2026-09-23",
        "appeal-determined:2026-08-25:2026-09-23",
        "outstanding:appeals:true",
    ]

    with pytest.raises(devon.DevonCheckpointError, match="replay"):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DevonMock(shift_first_open=True)),
                window,
                checkpoint,
            )
        )


def test_camden_exact_resolution_and_public_package_collection() -> None:
    adapter = camden.CamdenAdapter()
    package = AuthorityPackage(
        adapter, camden.CamdenApplicationV1, camden_discovery.CamdenCheckpointV1
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
        "BR/156/25/PL",
        "BR/157/25/PL",
    ]
    assert resumed[-1].complete
    assert (
        sum(
            request.method == RequestMethod.POST for request in resumed_session.requests
        )
        == 3
    )

    with pytest.raises(arun.ArunParseError, match="partial result count"):
        asyncio.run(
            _batches(adapter, _Session(_ArunMock(first_reported=0)), window, None)
        )
    with pytest.raises(arun.ArunParseError, match="partial result count"):
        asyncio.run(
            _batches(adapter, _Session(_ArunMock(first_show_all=False)), window, None)
        )
    with pytest.raises(arun.ArunCountMismatchError):
        asyncio.run(
            _batches(
                adapter,
                _Session(
                    _ArunMock(
                        expanded_reported=1,
                        expanded_rows=("BR/156/25/PL",),
                    )
                ),
                window,
                None,
            )
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


def test_devon_window_disclaimer_pager_and_identity_boundaries() -> None:
    adapter = devon.DevonAdapter()
    assert devon._REDIRECT_BOUNDARY.allows(
        f"{devon.BASE_URL}/Disclaimer?returnUrl=%2FSearch%2FAdvanced"
    )
    assert not devon._REDIRECT_BOUNDARY.allows(
        f"{devon.BASE_URL}/Search/Results?unexpected=value"
    )
    window = DiscoveryWindow(
        start=date(2026, 8, 18), end=date(2026, 9, 16), include_open=False
    )
    with pytest.raises(devon.DevonWindowUnsupportedError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DevonMock()),
                window.model_copy(update={"start": date(2026, 8, 17)}),
                None,
            )
        )
    with pytest.raises(devon.DevonDisclaimerAcceptanceError):
        asyncio.run(
            _batches(
                adapter, _Session(_DevonMock(repeated_disclaimer=True)), window, None
            )
        )
    with pytest.raises(devon.DevonPaginationError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DevonMock(direct=True, malformed_page=1)),
                window.model_copy(update={"include_open": True}),
                None,
            )
        )
    stale = devon.DevonCheckpointV1(
        result_page="live",
        live_scope=devon.DevonDiscoveryScope(
            start=date(2020, 1, 1), end=date(2020, 1, 2), include_open=False
        ),
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
    with pytest.raises(devon.DevonRoutingError):
        asyncio.run(
            adapter.fetch(
                _Session(_DevonMock(direct=True)),
                reference.model_copy(update={"source_id": devon.APPEAL_SOURCE}),
            )
        )
    with pytest.raises(devon.DevonRoutingError):
        devon.parse_native_evidence(
            reference.model_copy(update={"locator": None}), _devon_detail()
        )
    with pytest.raises(devon.DevonRoutingError):
        devon.parse_native_evidence(
            reference.model_copy(update={"source_id": devon.APPEAL_SOURCE}),
            _devon_detail(),
        )

    class _CrossRouteSession(_Session):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            capture = await super().fetch(request)
            return capture.model_copy(
                update={
                    "url": HttpUrl(f"{devon.BASE_URL}/Appeals/Display/DCC/4473/2026")
                }
            )

    with pytest.raises(devon.DevonRoutingError):
        asyncio.run(
            adapter.fetch(_CrossRouteSession(_DevonMock(direct=True)), reference)
        )
    for locator in (
        "https://evil.test/Planning/Display/DCC/4473/2026",
        f"{devon.BASE_URL}/Document/Download?id=1",
    ):
        session = _Session(_DevonMock())
        with pytest.raises(devon.DevonProtectedRouteError):
            asyncio.run(
                adapter.fetch(
                    session, reference.model_copy(update={"locator": locator})
                )
            )
        assert session.requests == []

    malicious_disclaimer = _devon_disclaimer("/Search/Advanced").replace(
        b'action="/Disclaimer/Accept',
        b'action="https://evil.test/Disclaimer/Accept',
    )
    session = _Session(lambda _request: malicious_disclaimer)
    with pytest.raises(devon.DevonProtectedRouteError):
        asyncio.run(
            devon._fetch_protected(
                session,
                PortalRequest(
                    url=HttpUrl(devon._ADVANCED_FORM_URL),
                    intent=RequestIntent.SEARCH,
                ),
            )
        )
    assert len(session.requests) == 1
    with pytest.raises(devon.DevonProtectedRouteError):
        devon._validate_disclaimer_action(
            HttpUrl(
                f"{devon.BASE_URL}/Disclaimer/Accept"
                "?returnUrl=%2FSearch%2FAdvanced&unexpected=value"
            ),
            HttpUrl(devon._ADVANCED_FORM_URL),
        )
    accepted_action = HttpUrl(
        f"{devon.BASE_URL}/Disclaimer/Accept?returnUrl=%2FSearch%2FAdvanced"
    )
    assert not devon._REDIRECT_BOUNDARY.allows(str(accepted_action))
    assert devon._REDIRECT_BOUNDARY.model_copy(
        update={"exact_urls": (accepted_action,)}
    ).allows(str(accepted_action))
    with pytest.raises(devon.DevonProtectedRouteError):
        devon._validate_disclaimer_action(
            HttpUrl(
                f"{devon.BASE_URL}/Disclaimer/Accept?returnUrl=%2FSearch%2FResults"
            ),
            HttpUrl(devon._ADVANCED_FORM_URL),
        )

    class _RedirectingDisclaimerSession(_Session):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            capture = await super().fetch(request)
            if "/Disclaimer/Accept" in str(request.url):
                return capture.model_copy(
                    update={"url": HttpUrl(devon._ADVANCED_FORM_URL)}
                )
            return capture

    redirected = asyncio.run(
        devon._fetch_protected(
            _RedirectingDisclaimerSession(_DevonMock()),
            PortalRequest(
                url=HttpUrl(devon._ADVANCED_FORM_URL),
                intent=RequestIntent.SEARCH,
            ),
        )
    )
    assert redirected[-1].url == HttpUrl(devon._ADVANCED_FORM_URL)


def test_camden_discovery_search_document_and_identity_boundaries() -> None:
    adapter = camden.CamdenAdapter()
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
            references=(),
            initial_evidence=_SEARCH_DIGEST,
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
    rolled_session = _Session(_ArunMock())
    rolled = asyncio.run(
        _batches(
            adapter,
            rolled_session,
            window.model_copy(update={"include_open": True}),
            terminal,
        )
    )
    assert rolled[-1].complete
    assert rolled_session.requests

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
                initial_evidence=_SEARCH_DIGEST,
                seen_references=(),
            ),
        )
    )
    with pytest.raises(arun.ArunParseError, match="partial result count"):
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
            _arun_results(("A",), 1, show_all=False, fields={}).replace(
                b"planningDetails?reference=A&amp;from=planningSearch",
                b"planningDetails?from=planningSearch",
            )
        )
    with pytest.raises(arun.ArunParseError, match="duplicate result reference"):
        arun._parse_search_results(
            _arun_results(("A", "A"), 2, show_all=False, fields={})
        )
    assert arun._parse_search_results(_arun_empty_results()).reported == 0
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
    adapter = devon.DevonAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18), end=date(2026, 9, 16), include_open=False
    )
    scope = devon.DevonDiscoveryScope(
        start=window.start, end=window.end, include_open=False
    )
    distinct_routes = devon.DevonCheckpointV1(
        result_page="live",
        live_scope=scope,
        seen_references=(
            SourceReference(
                source_id=devon.PLANNING_SOURCE,
                reference="DCC/1",
                locator=f"{devon.BASE_URL}/Planning/Display/DCC/1",
            ),
            SourceReference(
                source_id=devon.APPEAL_SOURCE,
                reference="DCC/1",
                locator=f"{devon.BASE_URL}/Appeals/Display/DCC/1",
            ),
        ),
    )
    assert len(distinct_routes.seen_references) == 2
    terminal = devon.DevonCheckpointV1(
        result_page="live",
        live_scope=scope,
        completed_queries=devon._query_keys(scope),
        query_summaries=tuple(
            devon.DevonQuerySummaryV1(
                query_key=query_key,
                row_count=1,
                page_count=1,
            )
            for query_key in devon._query_keys(scope)
        ),
        seen_references=(
            SourceReference(
                source_id=devon.SOURCE,
                reference="DCC/4473/2026",
                locator=f"{devon.BASE_URL}/Planning/Display/DCC/4473/2026",
            ),
        ),
        live_complete=True,
    )
    terminal_session = _Session(_DevonMock())
    assert asyncio.run(_batches(adapter, terminal_session, window, terminal))[
        0
    ].complete
    assert terminal_session.requests == []
    with pytest.raises(devon.DevonParseError, match="accepted disclaimer"):
        devon._parse_discovery_page(_devon_disclaimer("search"), expected_page=1)
    with pytest.raises(devon.DevonParseError, match="detail link"):
        devon._parse_discovery_page(
            b'<dl class="searchResultsList"></dl>', expected_page=1
        )
    fallback = devon._parse_discovery_page(
        b'<dl class="searchResultsList"><a href="/Planning/Display/DCC/1"></a></dl>',
        expected_page=1,
    )
    assert fallback.references[0].reference == "DCC/1"
    appeal_result = devon._parse_discovery_page(
        _devon_results(("DCC/1",), route="Appeals"),
        expected_page=1,
        expected_source=devon.APPEAL_SOURCE,
    )
    assert fallback.references[0].source_id == devon.PLANNING_SOURCE
    assert appeal_result.references[0].source_id == devon.APPEAL_SOURCE
    assert fallback.references[0].reference == appeal_result.references[0].reference
    with pytest.raises(devon.DevonParseError, match="query result route"):
        devon._parse_discovery_page(
            _devon_results(("DCC/1",), route="Appeals"),
            expected_page=1,
        )
    with pytest.raises(devon.DevonParseError, match="appeal singleton locator"):
        devon._parse_discovery_page(
            _devon_appeal_detail("DCC/1"),
            expected_page=1,
            expected_source=devon.APPEAL_SOURCE,
        )
    appeal_singleton = devon._parse_discovery_page(
        _devon_appeal_detail("DCC/1"),
        expected_page=1,
        expected_source=devon.APPEAL_SOURCE,
        response_url=HttpUrl(f"{devon.BASE_URL}/Appeals/Display/DCC/1"),
    )
    assert appeal_singleton.references == (
        SourceReference(
            source_id=devon.APPEAL_SOURCE,
            reference="DCC/1",
            locator=f"{devon.BASE_URL}/Appeals/Display/DCC/1",
        ),
    )
    with pytest.raises(devon.DevonParseError, match="planning singleton locator"):
        devon._parse_discovery_page(
            _devon_detail(),
            expected_page=1,
            response_url=HttpUrl(
                "https://attacker.example/Planning/Display/DCC/4473/2026"
            ),
        )
    with pytest.raises(devon.DevonParseError, match="planning singleton locator"):
        devon._parse_discovery_page(
            _devon_detail(),
            expected_page=1,
            response_url=HttpUrl("https://attacker.example/Search/Results"),
        )
    with pytest.raises(devon.DevonParseError, match="planning singleton locator"):
        devon._parse_discovery_page(
            _devon_detail(),
            expected_page=1,
            response_url=HttpUrl(f"{devon.BASE_URL}/Search/Advanced"),
        )
    with pytest.raises(devon.DevonParseError, match="planning singleton locator"):
        devon._parse_discovery_page(
            _devon_detail(),
            expected_page=1,
            response_url=HttpUrl(f"{devon.BASE_URL}/Appeals/Display/DCC/4473/2026"),
        )
    with pytest.raises(devon.DevonParseError, match="planning singleton reference"):
        devon._parse_discovery_page(
            _devon_detail(),
            expected_page=1,
            response_url=HttpUrl(f"{devon.BASE_URL}/Planning/Display/WRONG/1"),
        )
    planning_singleton = devon._parse_discovery_page(
        _devon_detail(),
        expected_page=1,
        response_url=HttpUrl(f"{devon.BASE_URL}/Planning/Display/DCC/4473/2026"),
    )
    assert planning_singleton.references[0].locator == (
        f"{devon.BASE_URL}/Planning/Display/DCC/4473/2026"
    )
    assert (
        devon._parse_discovery_page(b"<p>No records</p>", expected_page=1).references
        == ()
    )
    with pytest.raises(devon.DevonParseError, match="no-records"):
        devon._parse_discovery_page(b"<p>Unknown</p>", expected_page=1)
    with pytest.raises(devon.DevonParseError, match="page-one singleton"):
        devon._parse_discovery_page(_devon_detail(), expected_page=2)
    pager = _devon_results(
        tuple(f"DCC/{number}/2026" for number in range(1, 11)),
        total_pages=2,
    )
    pager_only = pager[pager.index(b'<ul class="pagination">') :]
    with pytest.raises(devon.DevonPaginationError, match="singleton-detail-pager"):
        devon._parse_discovery_page(_devon_detail() + pager_only, expected_page=1)
    with pytest.raises(devon.DevonPaginationError):
        devon._parse_discovery_page(
            _devon_results(tuple(f"DCC/{number}/2026" for number in range(10))),
            expected_page=1,
        )
    with pytest.raises(devon.DevonParseError, match="details-grid"):
        devon._parse_labelled_fields(b'<dl class="details-grid"><dt>Orphan</dt></dl>')
    malformed_fields = devon._parse_labelled_fields(
        b'<dl class="details-grid"><dt>Proposal</dt><dd>Exact proposal'
        b"<dt>Location</dt><dd>Exact location</dd></dl>"
    )
    assert malformed_fields == {
        "proposal": "Exact proposal",
        "location": "Exact location",
    }
    assert devon._parse_coordinates(b"<html></html>") == (None, None)
    with pytest.raises(devon.DevonParseError, match="coordinates"):
        devon._parse_coordinates(b"<script>var easting = 300476;</script>")
    assert devon._parse_appeal_coordinates({}) == (None, None)
    with pytest.raises(devon.DevonParseError, match="appeal coordinates"):
        devon._parse_appeal_coordinates({"easting": "300476"})
    with pytest.raises(devon.DevonParseError, match="appeal coordinates"):
        devon._parse_appeal_coordinates({"easting": "invalid", "northing": "91039"})
    with pytest.raises(devon.DevonRoutingError):
        devon._detail_route(HttpUrl(f"{devon.BASE_URL}/Unknown/Display/DCC/1"))
    with pytest.raises(devon.DevonRoutingError):
        devon._detail_url_reference(
            HttpUrl(f"{devon.BASE_URL}/Appeals/Display/DCC/1"), "planning"
        )
    assert devon._parse_constraints(b"<html></html>") == ((), False)
    assert devon._parse_consultations(b"<html></html>") == ((), False)
    assert devon._parse_appeal_consultations(b"<html></html>") == ((), False)
    constraint_table = (
        b'<table summary="Planning Constraints"><thead><tr><th>Description</th>'
        b"</tr></thead><tbody><tr><td>Constraint</td></tr></tbody></table>"
    )
    with pytest.raises(devon.DevonParseError, match="constraints table"):
        devon._parse_constraints(constraint_table + constraint_table)
    with pytest.raises(devon.DevonParseError, match="constraints headers"):
        devon._parse_constraints(constraint_table.replace(b"Description", b"Other"))
    with pytest.raises(devon.DevonParseError, match="constraint row"):
        devon._parse_constraints(
            constraint_table.replace(b"<td>Constraint</td>", b"<td></td>")
        )
    consultation_table = (
        b'<table summary="Planning Consultees"><thead><tr>'
        b"<th>Consultee Name</th><th>Date Letter Sent</th>"
        b"<th>Consultation Expiry Date</th><th>Reply Received</th>"
        b"</tr></thead><tbody><tr><td>Consultee</td></tr></tbody></table>"
    )
    with pytest.raises(devon.DevonParseError, match="consultations table"):
        devon._parse_consultations(consultation_table + consultation_table)
    with pytest.raises(devon.DevonParseError, match="consultations headers"):
        devon._parse_consultations(
            consultation_table.replace(b"Consultee Name", b"Other")
        )
    with pytest.raises(devon.DevonParseError, match="consultation row"):
        devon._parse_consultations(
            consultation_table.replace(b"<td>Consultee</td>", b"<td></td>")
        )
    appeal_consultation_table = (
        b'<table summary="Appeal Consultees"><thead><tr>'
        b"<td>Consultee Name</td><td>Date Letter Sent</td>"
        b"<td>Consultation Expiry Date</td><td>Reply Received</td>"
        b"</tr></thead><tbody><tr><td>Consultee</td></tr></tbody></table>"
    )
    with pytest.raises(devon.DevonParseError, match="appeal consultations table"):
        devon._parse_appeal_consultations(
            appeal_consultation_table + appeal_consultation_table
        )
    with pytest.raises(devon.DevonParseError, match="appeal consultations headers"):
        devon._parse_appeal_consultations(
            appeal_consultation_table.replace(b"Consultee Name", b"Other")
        )
    with pytest.raises(devon.DevonParseError, match="appeal consultation row"):
        devon._parse_appeal_consultations(
            appeal_consultation_table.replace(b"<td>Consultee</td>", b"<td></td>")
        )
    assert devon._split_lines(None) == ()
    assert devon._optional_field({"second": "value"}, "first", "second") == "value"
    assert devon._optional_field({}, "missing") is None
    with pytest.raises(devon.DevonParseError, match="detail missing"):
        devon._required_field({}, "missing")
    assert devon._optional_date({}, "date") is None
    assert devon._optional_date({"date": "-"}, "date") is None
    with pytest.raises(devon.DevonParseError, match="date date"):
        devon._optional_date({"date": "bad"}, "date")


def test_devon_checkpoint_form_and_replay_fail_closed_boundaries() -> None:
    scope = devon.DevonDiscoveryScope(
        start=date(2026, 8, 18), end=date(2026, 9, 16), include_open=True
    )
    keys = devon._query_keys(scope)
    references = tuple(
        SourceReference(
            source_id=devon.SOURCE,
            reference=f"OPEN/{number:03d}/2026",
            locator=f"{devon.BASE_URL}/Planning/Display/OPEN/{number:03d}/2026",
        )
        for number in range(1, 11)
    )
    links = tuple(
        devon.DevonPageLinkV1(
            page=page,
            locator=HttpUrl(
                devon._RESULTS_URL if page == 1 else f"{devon._RESULTS_URL}/{page}"
            ),
        )
        for page in range(2, 7)
    )
    proof = devon.DevonPageProofV1(
        page=1,
        references=references,
        numbered_pages=(1, 2, 3, 4, 5, 6),
        numbered_links=links,
        next_locator=HttpUrl(f"{devon._RESULTS_URL}/2"),
    )
    completed_summaries = tuple(
        devon.DevonQuerySummaryV1(
            query_key=query_key,
            row_count=1,
            page_count=1,
        )
        for query_key in keys[:2]
    )

    def rejected(code: str, **changes: Any) -> None:
        values: dict[str, Any] = {
            "result_page": "live",
            "live_scope": scope,
            "completed_queries": keys[:2],
            "query_summaries": completed_summaries,
            "active_query": keys[2],
            "next_page": 2,
            "active_pages": (proof,),
            "seen_references": references,
            "live_complete": False,
        }
        values.update(changes)
        with pytest.raises(ValueError, match=code):
            devon.DevonCheckpointV1.model_validate(values)

    rejected("live-scope-required", live_scope=None)
    rejected("live-result-cursor-required", result_page="fixture")
    rejected("completed-query-prefix", completed_queries=(keys[1],))
    rejected("completed-query-summaries", query_summaries=completed_summaries[:1])
    rejected("seen-references", seen_references=(*references, references[0]))
    rejected(
        "seen-references",
        seen_references=(references[0].model_copy(update={"locator": None}),),
    )
    rejected("terminal-incoherent", live_complete=True)
    rejected(
        "terminal-flag-required",
        completed_queries=keys,
        query_summaries=tuple(
            devon.DevonQuerySummaryV1(
                query_key=query_key,
                row_count=1,
                page_count=1,
            )
            for query_key in keys
        ),
        active_query=None,
        next_page=1,
        active_pages=(),
    )
    rejected(
        "inactive-page-progress",
        active_query=None,
        next_page=2,
        active_pages=(),
    )
    rejected("active-query-incoherent", active_query=keys[1])
    rejected("active-query-incoherent", next_page=3)
    rejected("active-query-incoherent", active_pages=(), next_page=1)
    rejected(
        "active-page-incoherent",
        active_pages=(proof.model_copy(update={"page": 2}),),
    )
    rejected(
        "active-page-incoherent",
        active_pages=(proof.model_copy(update={"references": references[:-1]}),),
    )
    with pytest.raises(devon.DevonPaginationError, match="terminal-continuation"):
        devon._DiscoveryPage(
            references=references[:1],
            page=1,
            numbered_pages=(),
            numbered_links=(),
            next_locator=None,
            terminal=True,
        ).committed_proof()

    terminal_scope = scope.model_copy(update={"include_open": False})
    terminal = devon.DevonCheckpointV1(
        result_page="live",
        live_scope=terminal_scope,
        completed_queries=devon._query_keys(terminal_scope),
        query_summaries=tuple(
            devon.DevonQuerySummaryV1(
                query_key=query_key,
                row_count=1,
                page_count=1,
            )
            for query_key in devon._query_keys(terminal_scope)
        ),
        seen_references=references,
        live_complete=True,
    )
    with pytest.raises(devon.DevonCheckpointError, match="qualification-scope"):
        devon.qualification_audit(
            terminal,
            DiscoveryWindow(start=scope.start, end=scope.end, include_open=True),
        )

    progress = devon.DevonCheckpointV1(
        result_page="live",
        live_scope=scope,
        completed_queries=keys[:2],
        query_summaries=completed_summaries,
        active_query=keys[2],
        next_page=2,
        active_pages=(proof,),
        seen_references=references,
    )
    query = devon._query_inventory(scope)[2]
    terminal_page = devon._DiscoveryPage(
        references=(),
        page=1,
        numbered_pages=(),
        numbered_links=(),
        next_locator=None,
        terminal=True,
    )
    with pytest.raises(devon.DevonCheckpointError, match="page-cursor"):
        devon._advance_checkpoint(
            progress, query=query, page=terminal_page, all_query_keys=keys
        )
    changed = references[0].model_copy(
        update={"locator": f"{devon.BASE_URL}/Planning/Display/changed"}
    )
    with pytest.raises(devon.DevonCheckpointError, match="reference-locator"):
        devon._advance_checkpoint(
            devon.DevonCheckpointV1(
                result_page="live",
                live_scope=scope,
                completed_queries=keys[:2],
                query_summaries=completed_summaries,
                seen_references=references,
            ),
            query=query,
            page=terminal_page.model_copy(update={"references": (changed,)}),
            all_query_keys=keys,
        )
    with pytest.raises(devon.DevonCheckpointError, match="query-duplicate"):
        devon._advance_checkpoint(
            progress,
            query=query,
            page=devon._DiscoveryPage(
                references=(references[0],),
                page=2,
                numbered_pages=proof.numbered_pages,
                numbered_links=links,
                next_locator=HttpUrl(f"{devon._RESULTS_URL}/3"),
                terminal=False,
            ),
            all_query_keys=keys,
        )
    with pytest.raises(devon.DevonCheckpointError, match="pager-inventory"):
        devon._advance_checkpoint(
            progress,
            query=query,
            page=devon._DiscoveryPage(
                references=(
                    SourceReference(
                        source_id=devon.SOURCE,
                        reference="OPEN/011/2026",
                        locator=f"{devon.BASE_URL}/Planning/Display/OPEN/011/2026",
                    ),
                ),
                page=2,
                numbered_pages=(),
                numbered_links=(),
                next_locator=None,
                terminal=True,
            ),
            all_query_keys=keys,
        )
    with pytest.raises(devon.DevonCheckpointError, match="pager-terminal"):
        devon._advance_checkpoint(
            progress,
            query=query,
            page=devon._DiscoveryPage(
                references=(
                    SourceReference(
                        source_id=devon.SOURCE,
                        reference="OPEN/011/2026",
                        locator=f"{devon.BASE_URL}/Planning/Display/OPEN/011/2026",
                    ),
                ),
                page=2,
                numbered_pages=proof.numbered_pages,
                numbered_links=links,
                next_locator=None,
                terminal=True,
            ),
            all_query_keys=keys,
        )

    with pytest.raises(devon.DevonParseError, match="advanced form"):
        devon._parse_advanced_form(b"<html></html>")
    with pytest.raises(devon.DevonParseError, match="advanced form action"):
        devon._parse_advanced_form(
            _devon_advanced_form().replace(b'/Search/Results"', b'/wrong"')
        )
    with pytest.raises(devon.DevonParseError, match="advanced form controls"):
        devon._parse_advanced_form(
            _devon_advanced_form().replace(b'<input name="Address" value="old">', b"")
        )
    with pytest.raises(devon.DevonParseError, match="advanced boolean controls"):
        devon._parse_advanced_form(
            _devon_advanced_form().replace(
                b'name="Outstanding" value="true"',
                b'name="Outstanding" value="yes"',
            )
        )
    form = devon._parse_advanced_form(_devon_advanced_form())
    with pytest.raises(devon.DevonCheckpointError, match="dated-query-bounds"):
        devon._advanced_fields(
            form,
            devon._DevonQuery(kind="received", key="received:missing"),
        )
    address = form.select_one('input[name="Address"]')
    assert address is not None
    cast("Any", address)["name"] = ["not-string"]
    parish = form.select_one('select[name="Parish"]')
    assert parish is not None
    parish.clear()
    textarea = BeautifulSoup('<textarea name="Notes">value</textarea>', "html.parser")
    notes = textarea.select_one("textarea")
    assert notes is not None
    form.append(notes)
    extras = BeautifulSoup(
        '<input type="submit" name="Ignored" value="Search">'
        '<select name="EmptyChoice"></select>'
        '<select name="FirstChoice"><option value="first">First</option></select>'
        '<select name="SelectedChoice"><option value="selected" selected>Selected</option></select>',
        "html.parser",
    )
    for extra in extras.select("input, select"):
        form.append(extra)
    fields = devon._advanced_fields(form, query)
    pairs = tuple((field.name, field.value) for field in fields)
    assert ("Parish", "") in pairs
    assert ("Notes", "value") in pairs
    assert ("EmptyChoice", "") in pairs
    assert ("FirstChoice", "first") in pairs
    assert ("SelectedChoice", "selected") in pairs
    assert all(name != "Ignored" for name, _ in pairs)
    appeal_form = devon._parse_advanced_form(_devon_advanced_form())
    appeal_fields = devon._advanced_fields(
        appeal_form,
        devon._DevonQuery(
            kind="appeal-received",
            key="appeal-received:2026-08-18:2026-09-16",
            start=scope.start,
            end=scope.end,
        ),
    )
    appeal_pairs = tuple((field.name, field.value) for field in appeal_fields)
    assert ("SearchPlanning", "true") not in appeal_pairs
    assert ("SearchAppeals", "true") in appeal_pairs
    assert ("DateAppealFrom", "18/08/2026") in appeal_pairs
    assert ("DateAppealTo", "16/09/2026") in appeal_pairs

    dated_query = devon._DevonQuery(
        kind="received",
        key="received:2026-08-18:2026-09-16",
        start=scope.start,
        end=scope.end,
    )
    dated_form = devon._advanced_fields(
        devon._parse_advanced_form(_devon_advanced_form()), dated_query
    )
    request = devon.DevonDiscoveryRequestV1(
        url=f"{devon.BASE_URL}/Search/Results",
        method="POST",
        form=tuple((field.name, field.value) for field in dated_form),
    )
    expected_url = HttpUrl(f"{devon.BASE_URL}/Search/Results")
    assert devon.discovery_request_matches(dated_query, 1, request, expected_url)
    assert devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(
            update={
                "form": tuple(
                    (name, "True") if name == "AdvancedSearch" else (name, value)
                    for name, value in request.form
                )
            }
        ),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(update={"url": f"{devon.BASE_URL}/Search/Advanced"}),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query, 1, request.model_copy(update={"method": "GET"}), expected_url
    )
    assert not devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(update={"form": request.form[:-1]}),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(
            update={
                "form": tuple(
                    (name, "")
                    if name == "__RequestVerificationToken"
                    else (name, value)
                    for name, value in request.form
                )
            }
        ),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(
            update={
                "form": tuple(
                    (name, "false") if name == "AdvancedSearch" else (name, value)
                    for name, value in request.form
                )
            }
        ),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query,
        1,
        request.model_copy(
            update={
                "form": tuple(
                    (name, "false")
                    if name == "SearchPlanning" and value == "true"
                    else (name, value)
                    for name, value in request.form
                )
            }
        ),
        expected_url,
    )
    assert not devon.discovery_request_matches(
        dated_query.model_copy(update={"start": None}), 1, request, expected_url
    )
    continuation = devon.DevonDiscoveryRequestV1(
        url=f"{devon.BASE_URL}/Search/Results?page=2", method="GET", form=()
    )
    assert devon.discovery_request_matches(
        dated_query,
        2,
        continuation,
        HttpUrl(f"{devon.BASE_URL}/Search/Results?page=2"),
    )
    assert not devon.discovery_request_matches(
        dated_query,
        2,
        continuation.model_copy(update={"method": "POST"}),
        HttpUrl(f"{devon.BASE_URL}/Search/Results?page=2"),
    )

    adapter = devon.DevonAdapter()
    window = DiscoveryWindow(start=scope.start, end=scope.end, include_open=True)

    async def two_open_pages() -> Any:
        batches = cast(
            "AsyncGenerator[Any]",
            adapter.discover(_Session(_DevonMock(direct=True)), window, None),
        )
        for _ in range(3):
            await anext(batches)
        fourth = await anext(batches)
        await batches.aclose()
        return fourth.next_checkpoint

    two_page_checkpoint = asyncio.run(two_open_pages())
    replayed = asyncio.run(
        _batches(
            adapter,
            _Session(_DevonMock(direct=True)),
            window,
            two_page_checkpoint,
        )
    )
    assert replayed[-1].complete


def test_devon_result_and_pager_fail_closed_boundaries() -> None:
    one = _devon_results(("DCC/1/2026",))
    with pytest.raises(devon.DevonPaginationError, match="pager-host"):
        devon._parse_discovery_page(
            one,
            expected_page=1,
            response_url=HttpUrl("https://attacker.example/Search/Results"),
        )
    with pytest.raises(devon.DevonPaginationError, match="response-page-mismatch"):
        devon._parse_discovery_page(
            one,
            expected_page=1,
            response_url=HttpUrl(f"{devon.BASE_URL}/Search/Results/2"),
        )
    with pytest.raises(devon.DevonParseError, match="mixed result"):
        devon._parse_discovery_page(one + _devon_detail(), expected_page=1)
    with pytest.raises(devon.DevonParseError, match="duplicate result"):
        devon._parse_discovery_page(
            _devon_results(("DCC/1/2026", "DCC/1/2026")), expected_page=1
        )
    pager = _devon_results(
        tuple(f"DCC/{number}/2026" for number in range(1, 11)),
        total_pages=2,
    )
    pager_only = pager[pager.index(b'<ul class="pagination">') :]
    with pytest.raises(devon.DevonPaginationError, match="multiple-pagers"):
        devon._parse_discovery_page(pager + pager_only, expected_page=1)
    with pytest.raises(devon.DevonPaginationError, match="current-page-mismatch"):
        devon._parse_discovery_page(
            _devon_results(("DCC/1/2026",), page=2, total_pages=2),
            expected_page=1,
        )
    with pytest.raises(devon.DevonPaginationError, match="nonterminal-page-size"):
        devon._parse_discovery_page(
            _devon_results(
                tuple(f"DCC/{number}/2026" for number in range(1, 10)),
                total_pages=2,
            ),
            expected_page=1,
        )
    with pytest.raises(devon.DevonPaginationError, match="terminal-page-size"):
        devon._parse_discovery_page(
            _devon_results(
                tuple(f"DCC/{number}/2026" for number in range(1, 12)),
                page=2,
                total_pages=2,
            ),
            expected_page=2,
        )
    with pytest.raises(devon.DevonParseError, match="detail locator"):
        devon._parse_discovery_page(
            b'<dl class="searchResultsList"><a href="https://evil.test/Planning/Display/DCC/1">DCC/1</a></dl>',
            expected_page=1,
        )

    def parsed_pager(markup: str) -> Any:
        found = BeautifulSoup(markup, "html.parser").select_one("ul")
        assert found is not None
        return found

    cases = (
        ("current-page-marker", '<ul><li class="active">current</li></ul>'),
        (
            "numbered-locator",
            '<ul><li class="active">1</li><li><a href="/Search/Results/3">2</a></li><li><a rel="next" href="/Search/Results/2">Next</a></li></ul>',
        ),
        (
            "duplicate-numbered-link",
            '<ul><li class="active">1</li><li><a href="/Search/Results/2">2</a></li><li><a href="/Search/Results/2">2</a></li><li><a rel="next" href="/Search/Results/2">Next</a></li></ul>',
        ),
        (
            "nonconsecutive-numbering",
            '<ul><li class="active">1</li><li><a href="/Search/Results/3">3</a></li><li><a rel="next" href="/Search/Results/2">Next</a></li></ul>',
        ),
        (
            "linked-current-page",
            '<ul><li class="active">1</li><li><a href="/Search/Results">1</a></li></ul>',
        ),
        (
            "duplicate-forward-link",
            '<ul><li class="active">1</li><li><a href="/Search/Results/2">2</a></li><li><a rel="next" href="/Search/Results/2">Next</a></li><li><a href="/Search/Results/2">Next</a></li></ul>',
        ),
        (
            "nonterminal-forward-link",
            '<ul><li class="active">1</li><li><a href="/Search/Results/2">2</a></li><li><a href="/Search/Results/9">Other</a></li></ul>',
        ),
        (
            "terminal-forward-link",
            '<ul><li><a href="/Search/Results">1</a></li><li aria-current="page">2</li><li><a rel="next" href="/Search/Results/3">Forward</a></li></ul>',
        ),
    )
    for code, markup in cases:
        with pytest.raises(devon.DevonPaginationError, match=code):
            devon._parse_pager(parsed_pager(markup))

    with pytest.raises(devon.DevonPaginationError, match="pager-host"):
        devon._page_from_locator(HttpUrl("https://evil.test/Search/Results/2"))
    with pytest.raises(devon.DevonPaginationError, match="pager-parameters"):
        devon._page_from_locator(HttpUrl(f"{devon._RESULTS_URL}/2?bad=true"))
    with pytest.raises(devon.DevonPaginationError, match="pager-locator"):
        devon._page_from_locator(HttpUrl(f"{devon.BASE_URL}/wrong"))
    assert devon._query_bool({}, "isPlan") is None
    with pytest.raises(devon.DevonParseError, match="document isPlan"):
        devon._query_bool({"isPlan": ["maybe"]}, "isPlan")
    documents, document_state = devon._parse_documents(_devon_detail())
    assert len(documents) == 2
    assert document_state.kind == "complete"
    unavailable_documents, unavailable_state = devon._parse_documents(b"<html></html>")
    assert unavailable_documents == ()
    assert unavailable_state.kind == "unavailable"
    with pytest.raises(devon.DevonParseError, match="document section"):
        devon._parse_documents(_devon_detail().replace(b'id="PlanningdocTable"', b""))
    with pytest.raises(devon.DevonParseError, match="document locator"):
        devon._parse_documents(
            _devon_detail().replace(b"/Document/Download", b"/changed")
        )
    with pytest.raises(devon.DevonParseError, match="document locator"):
        devon._parse_documents(
            _devon_detail().replace(
                b'href="/Document/Download',
                b'href="https://evil.test/Document/Download',
                1,
            )
        )
    with pytest.raises(devon.DevonParseError, match="document row"):
        devon._parse_documents(
            _devon_detail().replace(b'href="/Document/Download', b'data-href="x', 1)
        )
    with pytest.raises(devon.DevonParseError, match="document headers"):
        devon._parse_documents(_devon_detail().replace(b"Created date", b"Uploaded", 1))
    with pytest.raises(devon.DevonParseError, match="document rows"):
        devon._parse_documents(
            b'<div id="PlanningdocTable"></div><table class="document-list">'
            b"<thead><tr><th>All</th><th>Description</th><th>Created date</th>"
            b"</tr></thead></table>"
        )
    with pytest.raises(devon.DevonParseError, match="document group"):
        devon._parse_documents(
            _devon_detail().replace(b'class="header active"', b'class="active"', 1)
        )
    with pytest.raises(devon.DevonParseError, match="document category"):
        devon._parse_documents(_devon_detail().replace(b"PLANS &amp; DRAWINGS", b"", 1))
    with pytest.raises(devon.DevonParseError, match="document row"):
        devon._parse_documents(
            _devon_detail().replace(
                b"<td>20/08/2026</td>",
                b"<td>20/08/2026</td><td>unexpected</td>",
                1,
            )
        )
    with pytest.raises(devon.DevonParseError, match="document date"):
        devon._parse_documents(
            _devon_detail().replace(b"<td>20/08/2026</td>", b"<td>bad</td>", 1)
        )
    with pytest.raises(devon.DevonParseError, match="document locator"):
        devon._parse_documents(
            _devon_detail().replace(
                b"/Document/Download?",
                b"/Document/DownloadExtra?",
                1,
            )
        )


def test_devon_qualification_requires_exact_safe_scope(tmp_path: Path) -> None:
    module = _devon_qualification_module()
    base = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "qualification"),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    variants = (
        (base[1:], "confirmation-required"),
        (base[:-1], "include-open-required"),
        ((*base[:4], "bad", *base[5:]), "invalid-date"),
        ((*base[:4], "2026-08-17", *base[5:]), "exact-window-required"),
    )
    for arguments, code in variants:
        with pytest.raises(module.QualificationConfigError, match=code):
            module._config(arguments)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("present")
    with pytest.raises(module.QualificationConfigError, match="resume-required"):
        module._config(
            [
                *base[:2],
                str(occupied),
                *base[3:],
            ]
        )


def test_devon_qualification_persists_typed_receipt_and_zero_network_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _devon_qualification_module()
    data_dir = tmp_path / "qualification"
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
    sessions: list[_Session] = []

    def session_factory() -> _Session:
        session = _Session(_DevonMock())
        sessions.append(session)
        return session

    assert (
        module.main(
            arguments,
            session_factory=session_factory,
            now=lambda: module.datetime(2026, 9, 16, 12, tzinfo=module.UTC),
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    receipt_path = data_dir / "devon-qualification-v5.json"
    receipt = json.loads(receipt_path.read_text())
    assert output == receipt
    assert receipt["schema_version"] == 5
    assert receipt["authority_id"] == "devon"
    assert receipt["scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    expected_queries = [
        "received:2026-08-18:2026-09-16",
        "determined:2026-08-18:2026-09-16",
        "outstanding:planning:true",
        "appeal-received:2026-08-18:2026-09-16",
        "appeal-determined:2026-08-18:2026-09-16",
        "outstanding:appeals:true",
    ]
    assert receipt["expected_queries"] == expected_queries
    assert receipt["completed_queries"] == expected_queries
    assert receipt["query_summaries"] == [
        {
            "query_key": expected_queries[0],
            "row_count": 3,
            "page_count": 1,
        },
        {
            "query_key": expected_queries[1],
            "row_count": 1,
            "page_count": 1,
        },
        {
            "query_key": expected_queries[2],
            "row_count": 55,
            "page_count": 6,
        },
        {
            "query_key": expected_queries[3],
            "row_count": 0,
            "page_count": 1,
        },
        {
            "query_key": expected_queries[4],
            "row_count": 0,
            "page_count": 1,
        },
        {
            "query_key": expected_queries[5],
            "row_count": 1,
            "page_count": 1,
        },
    ]
    assert receipt["counts"] == {
        "applications": 59,
        "discovered_references": 59,
        "native_versions": 59,
        "application_versions": 59,
        "document_versions": 59,
        "comment_versions": 0,
        "pending_retries": 0,
        "failed_sections": 0,
        "unmapped_records": 0,
    }
    assert receipt["costs"]["initial"]["request_count"] > 0
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert receipt["weekly_cycles"] == [
        {"sequence": 1, "due_on": "2026-09-23", "status": "pending"},
        {"sequence": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert receipt["operational_status"] == "pending-weekly-cycles"
    assert receipt["registry_promotion"] == "not-performed"
    assert {check["name"] for check in receipt["checks"]} == {
        "terminal-checkpoint-coherence",
        "exact-query-inventory",
        "durable-reference-application-agreement",
        "pending-retries",
        "failed-current-sections",
        "attachment-policy",
        "database-integrity",
        "evidence-integrity",
        "discovery-evidence",
        "unmapped-records",
        "idempotent-rerun",
        "terminal-rerun-network-io",
        "run-statuses",
    }
    assert all(check["ok"] for check in receipt["checks"])
    assert len(sessions) == 2
    assert sessions[0].requested_urls
    assert sessions[1].requested_urls == ()
    assert not (data_dir / ".devon-qualification-v5.json.tmp").exists()
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        retained_status = next(
            connection.execute(
                "SELECT live_readiness, live_reason FROM authorities "
                "WHERE authority_id = 'devon'"
            )
        )
        discovery_pages = tuple(
            connection.execute(
                "SELECT DISTINCT query_key, page FROM discovery_evidence "
                "ORDER BY query_key, page"
            )
        )
        source_counts = tuple(
            connection.execute(
                "SELECT source_id, COUNT(*) FROM applications "
                "GROUP BY source_id ORDER BY source_id"
            )
        )
        retained_evidence = next(connection.execute("SELECT COUNT(*) FROM evidence"))[0]
    assert len(discovery_pages) == 11
    assert retained_evidence > receipt["counts"]["applications"]
    assert source_counts == (
        ("devon-appeal-register", 1),
        ("devon-planning-register", 58),
    )
    assert retained_status == (
        "discovery-only",
        (
            "exact planning and appeal discovery is live-qualified; "
            "two later weekly cycles remain pending"
        ),
    )

    resumed_sessions: list[_Session] = []

    def resumed_factory() -> _Session:
        session = _Session(_DevonMock())
        resumed_sessions.append(session)
        return session

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=resumed_factory,
            now=lambda: module.datetime(2026, 9, 16, 13, tzinfo=module.UTC),
        )
        == 0
    )
    resumed_output = json.loads(capsys.readouterr().out)
    assert len(resumed_sessions) == 2
    assert all(session.requested_urls == () for session in resumed_sessions)
    assert resumed_output == receipt
    assert json.loads(receipt_path.read_text()) == receipt

    receipt_path.unlink()
    session_count = len(resumed_sessions)
    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=resumed_factory,
            now=lambda: module.datetime(2026, 9, 16, 14, tzinfo=module.UTC),
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["preserved-live-receipt"],
    }
    assert len(resumed_sessions) == session_count
    assert not receipt_path.exists()


def test_devon_qualification_reconciles_registered_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _devon_qualification_module()
    data_dir = tmp_path / "qualification"
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

    def session_factory() -> _Session:
        return _Session(_DevonMock())

    assert module.main(arguments, session_factory=session_factory) == 0
    capsys.readouterr()

    residual = data_dir / "evidence" / "interrupted.tmp"
    residual.write_bytes(b"partial")
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["evidence-integrity"],
    }
    residual.unlink()

    body = b"unregistered evidence"
    digest = sha256(body).hexdigest()
    orphan = data_dir / "evidence" / digest[:2] / f"{digest}.gz"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(gzip.compress(body))
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["evidence-integrity"],
    }

    orphan.unlink()

    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute(
            """
            INSERT INTO evidence(digest, path, source_url, media_type)
            VALUES (?, ?, ?, ?)
            """,
            (
                "0" * 64,
                f"00/{'0' * 64}.gz",
                devon.BASE_URL,
                "text/html",
            ),
        )
        connection.commit()
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["evidence-integrity"],
    }

    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute("DELETE FROM evidence WHERE digest = ?", ("0" * 64,))
        connection.execute(
            """
            UPDATE native_rebuild_inputs
            SET evidence_digests_json = ?
            WHERE application_id = (
                SELECT application_id FROM native_rebuild_inputs
                ORDER BY application_id LIMIT 1
            )
            """,
            (json.dumps(["0" * 64]),),
        )
        connection.commit()
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["evidence-integrity"],
    }


def test_devon_qualification_resumes_an_interrupted_partial_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing receipt does not strand a durable nonterminal checkpoint."""
    module = _devon_qualification_module()
    data_dir = tmp_path / "qualification"
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

    class _FailFirstDetailSession(_Session):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            if request.intent == RequestIntent.DETAIL:
                raise SourceUnavailableError("interrupted detail")
            return await super().fetch(request)

    assert (
        module.main(
            arguments,
            session_factory=lambda: _FailFirstDetailSession(_DevonMock()),
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"] == "runtime-failure"
    assert not (data_dir / "devon-qualification-v5.json").exists()

    sessions: list[_Session] = []

    def resumed_factory() -> _Session:
        session = _Session(_DevonMock())
        sessions.append(session)
        return session

    assert module.main([*arguments, "--resume"], session_factory=resumed_factory) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 5
    assert receipt["costs"]["initial"]["request_count"] > 0
    assert sessions[0].requested_urls
    assert sessions[1].requested_urls == ()


def test_devon_qualification_rejects_tampered_attachment_cost(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A preserved receipt cannot contradict its zero-attachment check."""
    module = _devon_qualification_module()
    data_dir = tmp_path / "qualification"
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
    session_factory = lambda: _Session(_DevonMock())  # noqa: E731
    assert module.main(arguments, session_factory=session_factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "devon-qualification-v5.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["costs"]["initial"]["attachment_body_requests"] = 7
    receipt_path.write_text(json.dumps(receipt))

    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["preserved-live-receipt"],
    }


def test_devon_qualification_binds_evidence_to_its_subject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Valid evidence bodies cannot be reassigned to other records or pages."""
    module = _devon_qualification_module()
    data_dir = tmp_path / "qualification"
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
    session_factory = lambda: _Session(_DevonMock())  # noqa: E731
    assert module.main(arguments, session_factory=session_factory) == 0
    capsys.readouterr()

    database = data_dir / "yimby.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        discovery_response = connection.execute(
            "SELECT rowid, response_url FROM discovery_evidence "
            "WHERE query_key LIKE 'received:%' AND page = 1"
        ).fetchone()
        assert discovery_response is not None
        connection.execute(
            "UPDATE discovery_evidence SET response_url = ? WHERE rowid = ?",
            ("https://attacker.example/Search/Results", discovery_response[0]),
        )
        connection.commit()
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["discovery-evidence"],
    }
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE discovery_evidence SET response_url = ? WHERE rowid = ?",
            (discovery_response[1], discovery_response[0]),
        )
        connection.commit()

    with closing(sqlite3.connect(database)) as connection:
        observation_rows = tuple(
            connection.execute(
                """
                SELECT linked.rowid, linked.observation_id,
                    observation.application_id, linked.digest, evidence.path
                FROM observation_evidence AS linked
                JOIN observations AS observation
                    ON observation.id = linked.observation_id
                JOIN evidence ON evidence.digest = linked.digest
                ORDER BY linked.observation_id, linked.digest
                """
            )
        )
        details = tuple(
            row
            for row in observation_rows
            if b"Disclaimer/Accept"
            not in gzip.decompress((data_dir / "evidence" / row[4]).read_bytes())
        )
        first = details[0]
        second = next(row for row in details if row[1] != first[1])
        rebuild_digests = {
            row[0]: json.loads(row[1])
            for row in connection.execute(
                "SELECT application_id, evidence_digests_json "
                "FROM native_rebuild_inputs WHERE application_id IN (?, ?)",
                (first[2], second[2]),
            )
        }
        temporary_digest = "f" * 64
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (temporary_digest, first[0]),
        )
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (first[3], second[0]),
        )
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (second[3], first[0]),
        )
        first_digests = list(rebuild_digests[first[2]])
        second_digests = list(rebuild_digests[second[2]])
        first_digests[first_digests.index(first[3])] = second[3]
        second_digests[second_digests.index(second[3])] = first[3]
        connection.execute(
            "UPDATE native_rebuild_inputs SET evidence_digests_json = ? "
            "WHERE application_id = ?",
            (json.dumps(first_digests), first[2]),
        )
        connection.execute(
            "UPDATE native_rebuild_inputs SET evidence_digests_json = ? "
            "WHERE application_id = ?",
            (json.dumps(second_digests), second[2]),
        )
        connection.commit()
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["evidence-integrity"],
    }

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (temporary_digest, first[0]),
        )
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (second[3], second[0]),
        )
        connection.execute(
            "UPDATE observation_evidence SET digest = ? WHERE rowid = ?",
            (first[3], first[0]),
        )
        connection.execute(
            "UPDATE native_rebuild_inputs SET evidence_digests_json = ? "
            "WHERE application_id = ?",
            (json.dumps(rebuild_digests[first[2]]), first[2]),
        )
        connection.execute(
            "UPDATE native_rebuild_inputs SET evidence_digests_json = ? "
            "WHERE application_id = ?",
            (json.dumps(rebuild_digests[second[2]]), second[2]),
        )
        discovery_rows = tuple(
            connection.execute(
                """
                SELECT linked.rowid, linked.digest, evidence.path
                FROM discovery_evidence AS linked
                JOIN evidence ON evidence.digest = linked.digest
                WHERE linked.query_key LIKE 'received:%'
                    AND linked.page = 1
                ORDER BY linked.digest
                """
            )
        )
        first_discovery = next(
            row
            for row in discovery_rows
            if b"Disclaimer/Accept"
            not in gzip.decompress((data_dir / "evidence" / row[2]).read_bytes())
        )
        later_rows = tuple(
            connection.execute(
                """
                SELECT linked.rowid, linked.digest, evidence.path
                FROM discovery_evidence AS linked
                JOIN evidence ON evidence.digest = linked.digest
                WHERE linked.query_key = 'outstanding:planning:true'
                    AND linked.page = 2
                ORDER BY linked.digest
                """
            )
        )
        second_discovery = next(
            row
            for row in later_rows
            if b"Disclaimer/Accept"
            not in gzip.decompress((data_dir / "evidence" / row[2]).read_bytes())
        )
        temporary_digest = "f" * 64
        connection.execute(
            "UPDATE discovery_evidence SET digest = ? WHERE rowid = ?",
            (temporary_digest, first_discovery[0]),
        )
        connection.execute(
            "UPDATE discovery_evidence SET digest = ? WHERE rowid = ?",
            (first_discovery[1], second_discovery[0]),
        )
        connection.execute(
            "UPDATE discovery_evidence SET digest = ? WHERE rowid = ?",
            (second_discovery[1], first_discovery[0]),
        )
        connection.commit()
    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "qualification-failed",
        "failed_checks": ["discovery-evidence"],
    }


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
        b"<dl><dt>Reference</dt><dd>A/1</dd><dt>Proposal</dt><dd>Value</dd>"
        b"<dt>Orphan</dt></dl></div>"
    ) == {"reference": "A/1", "proposal": "Value"}
    with pytest.raises(camden.CamdenParseError, match="labelled values"):
        camden._parse_dataview(
            b'<div class="dataview"><table><tr><td>orphan</td></tr></table></div>'
        )
    assert camden._mapping_value({"second": "value"}, "first", "second") == "value"
    assert camden._mapping_value({}, "missing") is None
    assert camden._field_allowing_empty({"proposal": ""}, "proposal") == ""
    with pytest.raises(camden.CamdenParseError, match="detail missing"):
        camden._field_allowing_empty({}, "missing")
    with pytest.raises(camden.CamdenParseError, match="detail missing"):
        camden._required_field({}, "missing")
    with pytest.raises(camden.CamdenParseError, match="integer easting"):
        camden._required_integer({"easting": "bad"}, "easting")
    assert camden._parse_date(None) is None
    with pytest.raises(camden.CamdenParseError, match="document date"):
        camden._parse_date("bad")


def test_camden_official_detail_and_document_shapes() -> None:
    detail = b"""
    <div class="dataview"><h1>Details Page</h1></div>
    <div class="dataview"><h2>Documents</h2></div>
    <div class="dataview"><h2>Application Details</h2><ul>
      <li><div><span>Application Number</span>TP/TP/12531/180693</div></li>
      <li><div><span>Proposal</span>Historic proposal</div></li>
      <li><div><span>Current Status</span>REGISTERED</div></li>
      <li><div><span>Location Co ordinates</span>Easting 530748 Northing 182755</div></li>
    </ul></div>
    """
    fields = camden._parse_dataview(detail)
    assert fields["application number"] == "TP/TP/12531/180693"
    assert camden._coordinate_pair(fields) == (530748, 182755)
    assert camden._coordinate_pair(
        {
            "application number": "TP/TP/12531/180693",
            "proposal": "Historic proposal",
        }
    ) == (None, None)
    with pytest.raises(camden.CamdenParseError, match="coordinate pair"):
        camden._coordinate_pair({"location co ordinates": "Easting 530748"})

    documents = camden._parse_documents(
        b"""
        <table id="casefilesummary"><tr><td><label>Application No:</label></td><td>A/1</td></tr>
          <tr><td><label>Records:</label></td><td>1</td></tr></table>
        <table id="recordtable"><thead><tr><th>Date Created</th><th>Title</th><th>Document Type</th></tr></thead>
          <tbody><tr><td>12/07/2014 16:25:45</td>
            <td><a href="/CMWebDrawer/Record/1/file/document?inline">Application Form</a></td>
            <td><a href="/CMWebDrawer/Record/1/file/document?inline">Application Form</a></td>
          </tr></tbody></table>
        """
    )
    assert documents[0].created_at == datetime(2014, 7, 12, 16, 25, 45)  # noqa: DTZ001
    assert documents[0].created_date == date(2014, 7, 12)
    assert documents[0].document_type == "Application Form"

    reference = SourceReference(
        source_id=camden.SEARCH_SOURCE,
        reference="2026/2706/L",
        locator="681726",
    )
    snapshot = asyncio.run(
        camden.CamdenAdapter().fetch(
            _Session(_CamdenMock(missing_coordinates=True)), reference
        )
    )
    assert snapshot.payload.grid_easting is None
    assert snapshot.payload.grid_northing is None
    assert camden.CamdenAdapter().normalise(snapshot).metadata.location is None


def test_camden_preserves_empty_proposal_and_explicit_empty_documents() -> None:
    reference = SourceReference(
        source_id=camden.SEARCH_SOURCE,
        reference="2026/2706/L",
        locator="681726",
    )

    snapshot = asyncio.run(
        camden.CamdenAdapter().fetch(
            _Session(_CamdenMock(empty_fields=True)),
            reference,
        )
    )

    assert snapshot.payload.proposal == ""
    assert snapshot.completeness.documents.kind == "empty"
    assert camden.CamdenAdapter().normalise(snapshot).proposal == ""


def test_camden_native_shape_and_parser_fail_closed_branches() -> None:
    with pytest.raises(ValueError, match="date disagrees"):
        camden.CamdenDocumentV1(
            title="Notice",
            url="https://camdocs.camden.gov.uk/notice",  # type: ignore[arg-type]
            created_date=date(2026, 9, 15),
            created_at=datetime(2026, 9, 16, 12),  # noqa: DTZ001
        )
    with pytest.raises(ValueError, match="both present or both absent"):
        camden.CamdenApplicationV1(
            public_reference="A/1",
            proposal="Proposal",
            current_status="REGISTERED",
            grid_easting=530748,
            grid_northing=None,
            documents=(),
            comments=(),
        )

    live_checkpoint = camden_discovery.initial_live_checkpoint(
        DiscoveryWindow(
            start=date(2026, 8, 18),
            end=date(2026, 9, 16),
            include_open=True,
        )
    )
    with pytest.raises(
        camden_discovery.CamdenCheckpointModeError, match="live checkpoint"
    ):
        asyncio.run(
            _batches(
                camden.CamdenAdapter(),
                _Session(_CamdenMock(), mode=TransportMode.FIXTURE),
                DiscoveryWindow(
                    start=date(2026, 8, 18),
                    end=date(2026, 9, 16),
                    include_open=True,
                ),
                live_checkpoint,
            )
        )

    assert (
        camden._parse_documents(
            b"""
        <table id="casefilesummary"><tr><td>short</td></tr>
          <tr><td><label>Records:</label></td><td>0</td></tr></table>
        <table id="recordtable"><thead><tr><th>Title</th></tr></thead>
          <tbody><tr><td>no link</td></tr></tbody></table>
        """
        )
        == ()
    )
    assert (
        camden._reported_document_count(
            BeautifulSoup(
                '<table id="casefilesummary"></table><p>Total 2 records</p>',
                "html.parser",
            )
        )
        == 2
    )
    row = BeautifulSoup(
        "<table><thead><tr><th>Description</th></tr></thead>"
        "<tbody><tr><td>value</td></tr></tbody></table>",
        "html.parser",
    ).select_one("tbody tr")
    assert row is not None
    assert camden._row_cell(row, "title", "description") is not None
    assert camden._row_cell(row, "missing") is None

    fields = camden._parse_dataview(
        b"""
        <div class="dataview"><ul>
          <li><div>unlabelled</div></li>
          <li><div><span>Application Number</span>A/1<em></em></div></li>
          <li><div><span>Proposal</span>Value</div></li>
        </ul></div>
        """
    )
    assert fields["application number"] == "A/1"
    with pytest.raises(camden.CamdenParseError, match="duplicate detail label"):
        camden._parse_dataview(
            b'<div class="dataview"><dl><dt>Reference</dt><dd>A/1</dd>'
            b"<dt>Reference</dt><dd>A/2</dd></dl></div>"
        )

    assert camden._coordinate_pair({"location co ordinates": "Easting Northing"}) == (
        None,
        None,
    )
    with pytest.raises(camden.CamdenParseError, match="coordinate pair"):
        camden._coordinate_pair({"location co ordinates": "Easting 1 Northing"})
    with pytest.raises(camden.CamdenParseError, match="coordinate pair"):
        camden._coordinate_pair({"easting": "1"})
    with pytest.raises(camden.CamdenParseError, match="coordinate pair"):
        camden._coordinate_pair({"easting": "one", "northing": "2"})
    assert camden._parse_document_datetime(None) is None


async def _batches(
    adapter: Any, session: _Session, window: DiscoveryWindow, checkpoint: Any
) -> list[Any]:
    return [batch async for batch in adapter.discover(session, window, checkpoint)]
