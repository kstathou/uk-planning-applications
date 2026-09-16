# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, B009, C901, E501, EM102, PLR0911, PLR0912, PLR0913, PLR0915, PLR2004, TRY003

"""Authority-owned live IDOX boundaries for Cornwall, Durham, West Suffolk, and Leeds."""

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

import yimby.authorities.cornwall.adapter as cornwall_adapter
import yimby.authorities.durham.adapter as durham_adapter
import yimby.authorities.leeds.adapter as leeds_adapter
import yimby.authorities.west_suffolk.adapter as west_suffolk_adapter
from yimby import AuthorityId, Collector, DiscoveryWindow, pilot_registry
from yimby.domain import (
    ApplicationId,
    DurableDiscoveryBatch,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceId,
    SourceReference,
    StoredCheckpoint,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import ModuleType

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalRequest

WEEK = DiscoveryWindow(
    start=date(2026, 9, 14),
    end=date(2026, 9, 20),
    include_open=False,
)
LATER_WEEK = DiscoveryWindow(
    start=date(2026, 9, 21),
    end=date(2026, 9, 27),
    include_open=False,
)


@dataclass(frozen=True, slots=True)
class _Case:
    authority_id: AuthorityId
    module: ModuleType
    base_url: str
    source_id: SourceId
    references: tuple[str, str, str, str]
    locators: tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class _UncountedPageShape:
    name: str
    rows: tuple[tuple[str, str], ...]
    capacity: str | None = "10"
    current_page: str | None = "1"
    numbered_page: int | None = None
    unnumbered_page: bool = False
    repeated_page_action: bool = False


CASES = (
    _Case(
        authority_id=AuthorityId("cornwall"),
        module=cornwall_adapter,
        base_url=cornwall_adapter.BASE_URL,
        source_id=SourceId("cornwall-idox-public-access"),
        references=("PA26/06144", "PA26/06145", "PA26/06146", "PA26/06147"),
        locators=("CORN-A", "CORN-B", "CORN-C", "CORN-D"),
    ),
    _Case(
        authority_id=AuthorityId("durham"),
        module=durham_adapter,
        base_url=durham_adapter.BASE_URL,
        source_id=SourceId("durham-idox-public-access"),
        references=(
            "DM/26/02422/LB",
            "DM/26/02423/FPA",
            "DM/26/02424/FPA",
            "DM/26/02425/FPA",
        ),
        locators=("DUR-A", "DUR-B", "DUR-C", "DUR-D"),
    ),
    _Case(
        authority_id=AuthorityId("west-suffolk"),
        module=west_suffolk_adapter,
        base_url=west_suffolk_adapter.BASE_URL,
        source_id=SourceId("west-suffolk-idox-public-access"),
        references=(
            "DC/26/1388/TCA",
            "DC/26/1389/FUL",
            "DC/26/1390/FUL",
            "DC/26/1391/FUL",
        ),
        locators=("WEST-A", "WEST-B", "WEST-C", "WEST-D"),
    ),
    _Case(
        authority_id=AuthorityId("leeds"),
        module=leeds_adapter,
        base_url=leeds_adapter.BASE_URL,
        source_id=SourceId("leeds-idox-public-access"),
        references=("26/05013/FU", "26/05014/FU", "26/05015/FU", "26/05016/FU"),
        locators=("LEEDS-A", "LEEDS-B", "LEEDS-C", "LEEDS-D"),
    ),
)

_WEST_SUFFOLK_CASE = CASES[2]
_WEST_SUFFOLK_OPEN_REFERENCES = (
    ("DC/26/2001/FUL", "WEST-OPEN-A"),
    ("DC/26/2002/FUL", "WEST-OPEN-B"),
    ("DC/26/2003/FUL", "WEST-OPEN-C"),
    ("DC/26/2004/FUL", "WEST-OPEN-D"),
    ("DC/26/2005/FUL", "WEST-OPEN-E"),
    ("DC/26/2006/FUL", "WEST-OPEN-F"),
    ("DC/26/2007/FUL", "WEST-OPEN-G"),
    ("DC/26/2008/FUL", "WEST-OPEN-H"),
)

_PREFIXES = {
    AuthorityId("cornwall"): "Cornwall",
    AuthorityId("durham"): "Durham",
    AuthorityId("west-suffolk"): "WestSuffolk",
    AuthorityId("leeds"): "Leeds",
}


def _member(case: _Case, suffix: str) -> Any:
    return getattr(case.module, f"{_PREFIXES[case.authority_id]}{suffix}")


def _weekly_form(search_type: str = "Application") -> bytes:
    return f"""
    <form action="weeklyListResults.do?action=firstPage" method="post">
      <input type="hidden" name="_csrf" value="sanitised-token">
      <select name="searchCriteria.parish"><option value="" selected>All</option></select>
      <select name="searchCriteria.ward"><option value="" selected>All</option></select>
      <select name="week">
        <option value="bad">Bad</option>
        <option value="13/09/2026">Sunday</option>
        <option value="14/09/2026">Monday</option>
        <option value="21/09/2026">Following Monday</option>
      </select>
      <input type="hidden" name="dateType" value="DC_Validated">
      <input type="hidden" name="searchType" value="{search_type}">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="submit" name="submit" value="Search">
    </form>
    """.encode()


def _advanced_form(
    *,
    malformed: bool = False,
    missing_option: bool = False,
    missing_form: bool = False,
    wrong_action: bool = False,
    wrong_method: bool = False,
    missing_hidden: bool = False,
) -> bytes:
    if missing_form:
        return b"<p>Advanced search unavailable</p>"
    high_court_option = (
        ""
        if missing_option
        else '<option value="High Court Appeal Lodged">High Court</option>'
    )
    status_fields = (
        ""
        if malformed
        else f"""
      <select name="searchCriteria.caseStatus">
        <option value="" selected>All</option>
        <option value="Pending Consideration">Pending Consideration</option>
        <option value="Pending Decision">Pending Decision</option>
        <option value="Received Awaiting Registration">Awaiting Registration</option>
        <option value="Pending Appeal Decision">Pending Appeal Decision</option>
      </select>
      <select name="searchCriteria.appealStatus">
        <option value="" selected>All</option>
        <option value="Appeal lodged">Appeal lodged</option>
        <option value="Appeal Remitted to Secretary of State">Remitted</option>
        {high_court_option}
        <option value="Pending Appeal Decision">Pending Appeal Decision</option>
      </select>
        """
    )
    action = (
        "/online-applications/unknown.do"
        if wrong_action
        else "/online-applications/advancedSearchResults.do?action=firstPage"
    )
    method = "get" if wrong_method else "post"
    hidden_fields = (
        "" if missing_hidden else '<input type="hidden" name="searchType" value="">'
    )
    return f"""
    <form id="advancedSearchForm" method="{method}" action="{action}">
      <input type="hidden" name="_csrf" value="">
      <input name="searchCriteria.reference" value="">
      {status_fields}
      <input type="hidden" name="caseAddressType" value="">
      <input name="date(applicationReceivedStart)" value="">
      <input name="date(applicationReceivedEnd)" value="">
      <input name="date(applicationValidatedStart)" value="">
      <input name="date(applicationValidatedEnd)" value="">
      <input name="date(applicationCommitteeStart)" value="">
      <input name="date(applicationCommitteeEnd)" value="">
      <input name="date(applicationDecisionStart)" value="">
      <input name="date(applicationDecisionEnd)" value="">
      <input name="date(appealDecisionStart)" value="">
      <input name="date(appealDecisionEnd)" value="">
      {hidden_fields}
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="submit" name="submit" value="Search">
    </form>
    """.encode()


def _result_page(
    rows: tuple[tuple[str, str], ...],
    *,
    count: int,
    label: str = "Reference",
    count_text: str | None = None,
) -> bytes:
    rendered = "".join(
        (
            '<li class="searchresult">'
            f'<a href="applicationDetails.do?keyVal={locator}&amp;activeTab=summary">'
            "Details</a>"
            f"<p><span>{label}:</span><span>{reference}</span></p></li>"
        )
        for reference, locator in rows
    )
    count_markup = (
        f'<div data-result-count="{count}"></div>'
        if count_text is None
        else f'<span class="showing">{count_text}</span>'
    )
    return f"{count_markup}<ul>{rendered}</ul>".encode()


def _uncounted_result_page(
    rows: tuple[tuple[str, str], ...],
    *,
    label: str = "Ref. No",
    capacity: str | None = "10",
    current_page: str | None = "1",
    numbered_page: int | None = None,
    unnumbered_page: bool = False,
    repeated_page_action: bool = False,
) -> bytes:
    rendered = "".join(
        (
            '<li class="searchresult">'
            f'<a href="applicationDetails.do?keyVal={locator}&amp;activeTab=summary">'
            "Details</a>"
            f"<p>{label}: {reference}</p></li>"
        )
        for reference, locator in rows
    )
    page_control = (
        ""
        if current_page is None
        else (
            f'<input type="hidden" name="searchCriteria.page" value="{current_page}">'
        )
    )
    capacity_control = (
        ""
        if capacity is None
        else (
            '<select name="searchCriteria.resultsPerPage">'
            f'<option value="{capacity}" selected>{capacity}</option></select>'
        )
    )
    pagination = (
        (
            '<a href="pagedSearchResults.do?action=page&amp;searchCriteria.page='
            f'{numbered_page}">{numbered_page}</a>'
        )
        if numbered_page is not None
        else (
            '<a href="pagedSearchResults.do?action=page&amp;action=printPreview">'
            "Next</a>"
            if repeated_page_action
            else (
                '<a href="pagedSearchResults.do?action=page">Next</a>'
                if unnumbered_page
                else ""
            )
        )
    )
    return (
        f'<form id="searchResults">{page_control}{capacity_control}</form>'
        '<a href="pagedSearchResults.do?action=printPreview">Print</a>'
        f"<ul>{rendered}</ul>{pagination}"
    ).encode()


def _paginated_result_page(
    rows: tuple[tuple[str, str], ...],
    count_text: str,
) -> bytes:
    pager = f'<p class="pager"><span class="showing">{count_text}</span></p>'
    page = _uncounted_result_page(rows, capacity="10", numbered_page=2).decode()
    return f"{pager}{page}{pager}".encode()


def _result_page_with_showing_markers(
    rows: tuple[tuple[str, str], ...],
    count_texts: tuple[str, ...],
    *,
    current_page: str = "1",
    numbered_page: int = 2,
    visible_page: str | None = None,
    capacity: str | None = "10",
) -> bytes:
    visible = "" if visible_page is None else f"<strong>{visible_page}</strong>"
    markers = "".join(
        f'<p class="pager"><span class="showing">{count_text}</span>{visible}</p>'
        for count_text in count_texts
    )
    page = _uncounted_result_page(
        rows,
        capacity=capacity,
        current_page=current_page,
        numbered_page=numbered_page,
    ).decode()
    return f"{markers}{page}".encode()


def _result_page_with_legacy_count(
    rows: tuple[tuple[str, str], ...],
    count_text: str,
) -> bytes:
    page = _uncounted_result_page(rows, capacity="10", numbered_page=2).decode()
    return f"<p>{count_text}</p>{page}".encode()


def _ten_result_rows(case: _Case) -> tuple[tuple[str, str], ...]:
    return tuple(
        (f"{case.references[0]}-{index}", f"{case.locators[0]}-{index}")
        for index in range(1, 11)
    )


def _summary(reference: str, authority_id: AuthorityId) -> bytes:
    extra = {
        AuthorityId("cornwall"): "<tr><th>Parish</th><td>Truro</td></tr>",
        AuthorityId("durham"): (
            "<tr><th>Electoral Division</th><td>Durham South</td></tr>"
        ),
        AuthorityId("west-suffolk"): (
            "<tr><th>Ward</th><td>Abbeygate</td></tr>"
            "<tr><th>Parish</th><td>Bury St Edmunds</td></tr>"
        ),
    }.get(authority_id, "")
    return (
        '<table id="simpleDetailsTable">'
        f"<tr><th>Reference</th><td>{reference}</td></tr>"
        "<tr><th>Alternative Reference</th><td>PP-15200000</td></tr>"
        "<tr><th>Address</th><td>1 Sanitised Street</td></tr>"
        "<tr><th>Proposal</th><td>Install replacement windows</td></tr>"
        "<tr><th>Status</th><td>Registered</td></tr>"
        "<tr><th>Received Date</th><td>13/09/2026</td></tr>"
        "<tr><th>Validated Date</th><td>14/09/2026</td></tr>"
        "<tr><th>Decision Date</th><td>20/09/2026</td></tr>"
        f"{extra}</table>"
    ).encode()


def _advanced_detail_redirect(*, fault: str | None = None) -> bytes:
    reference, locator = _WEST_SUFFOLK_OPEN_REFERENCES[3]
    other_locator = "WEST-OPEN-OTHER" if fault == "ambiguous-detail" else locator
    first_link = (
        "applicationDetails.do?activeTab=summary"
        if fault == "invalid-detail-link"
        else f"applicationDetails.do?keyVal={locator}&amp;activeTab=summary"
    )
    extra_table = (
        '<table id="simpleDetailsTable"><tr><th>Reference</th>'
        "<td>OTHER</td></tr></table>"
        if fault == "multiple-detail-tables"
        else ""
    )
    mixed_result = (
        '<li class="searchresult">Unexpected result row</li>'
        if fault == "mixed-detail"
        else ""
    )
    no_results = "<p>No results found.</p>" if fault == "empty-detail" else ""
    return (
        '<table id="simpleDetailsTable">'
        f"<tr><th>Reference</th><td>{reference}</td></tr></table>"
        f"{extra_table}{mixed_result}{no_results}"
        f'<a href="{first_link}">Summary</a>'
        f'<a href="applicationDetails.do?keyVal={other_locator}&amp;activeTab=documents">'
        "Documents</a>"
    ).encode()


def _west_suffolk_open_page(
    query: tuple[str, str],
    page: int,
    fault: str | None,
) -> bytes:
    field, value = query
    weekly = _WEST_SUFFOLK_CASE
    open_rows = _WEST_SUFFOLK_OPEN_REFERENCES
    if field == "searchCriteria.caseStatus":
        if value == "Pending Consideration":
            if page == 1:
                rows = ((weekly.references[1], weekly.locators[1]), open_rows[0])
                if fault == "count-mismatch":
                    return _result_page(rows, count=1)
                return _result_page_with_showing_markers(
                    rows,
                    ("Showing 1-2 of 3", "Showing 1-2 of 3"),
                    current_page="",
                )
            if fault == "detail-on-page-two":
                return _advanced_detail_redirect()
            remaining_rows = () if fault == "stalled-pagination" else (open_rows[1],)
            if not remaining_rows:
                return _result_page(remaining_rows, count=3)
            return _result_page_with_showing_markers(
                remaining_rows,
                ("Showing 3-3 of 3", "Showing 3-3 of 3"),
                current_page="2",
                numbered_page=1,
            )
        if value == "Pending Decision":
            return _result_page(
                ((weekly.references[3], weekly.locators[3]), open_rows[2]),
                count=2,
            )
        if value == "Received Awaiting Registration":
            return b"<p>No results found.</p>"
        if value == "Pending Appeal Decision":
            return _advanced_detail_redirect(fault=fault)
    if field == "searchCriteria.appealStatus":
        if value == "Appeal lodged":
            return _uncounted_result_page(
                (
                    (weekly.references[0], weekly.locators[0]),
                    *open_rows[:6],
                ),
                current_page="",
            )
        if value in {
            "Appeal Remitted to Secretary of State",
            "High Court Appeal Lodged",
        }:
            return b"<p>No results found.</p>"
        if value == "Pending Appeal Decision":
            if page == 1:
                return _result_page_with_showing_markers(
                    (open_rows[3], open_rows[6]),
                    ("Showing 1-2 of 3", "Showing 1-2 of 3"),
                    current_page="",
                )
            return _result_page_with_showing_markers(
                (open_rows[7],),
                ("Showing 3-3 of 3", "Showing 3-3 of 3"),
                current_page="2",
                numbered_page=1,
            )
    raise AssertionError(f"unexpected West Suffolk open query {query!r} page {page}")


def _expected_advanced_fields(
    target_field: str,
    value: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("_csrf", ""),
        ("searchCriteria.reference", ""),
        (
            "searchCriteria.caseStatus",
            value if target_field == "searchCriteria.caseStatus" else "",
        ),
        (
            "searchCriteria.appealStatus",
            value if target_field == "searchCriteria.appealStatus" else "",
        ),
        ("caseAddressType", ""),
        ("date(applicationReceivedStart)", ""),
        ("date(applicationReceivedEnd)", ""),
        ("date(applicationValidatedStart)", ""),
        ("date(applicationValidatedEnd)", ""),
        ("date(applicationCommitteeStart)", ""),
        ("date(applicationCommitteeEnd)", ""),
        ("date(applicationDecisionStart)", ""),
        ("date(applicationDecisionEnd)", ""),
        ("date(appealDecisionStart)", ""),
        ("date(appealDecisionEnd)", ""),
        ("searchType", ""),
        ("tag", "one"),
        ("tag", "two"),
    )


DOCUMENTS = b"""
<h2 data-section="documents" data-count="2">Documents (2)</h2>
<table summary="Documents"><thead><tr>
<th>Date Published</th><th>Document Type</th><th>Drawing Number</th>
<th>Description</th><th>View</th></tr></thead><tbody>
<tr><td>15/09/2026</td><td>Plan</td><td>A-01</td><td>Site plan</td>
<td><a href="viewer?id=1">Viewer</a><a href="files/site-plan.pdf">PDF</a></td></tr>
<tr><td>16/09/2026</td><td>Report</td><td></td><td></td>
<td><a href="files/report.tif">Image</a></td></tr>
</tbody></table>
"""


class _IdoxMock:
    def __init__(
        self,
        case: _Case,
        *,
        mismatch: bool = False,
        empty_stall: bool = False,
        documents_fail: bool = False,
        documents_malformed: bool = False,
        comments_fail: bool = False,
        comments_malformed: bool = False,
        comments_nonzero: bool = False,
        summary_mismatch: bool = False,
        leeds_unexpected_detail: bool = False,
        search_type: str = "Application",
        uncounted_terminal: bool = False,
        uncounted_label: str = "Ref. No",
        showing_counts: bool = False,
        validated_showing_markers: tuple[str, ...] | None = None,
        validated_legacy_count: str | None = None,
        expected_week: str = "14/09/2026",
        validated_current_page: str = "1",
        west_suffolk_open_fault: str | None = None,
    ) -> None:
        self.case = case
        self.mismatch = mismatch
        self.empty_stall = empty_stall
        self.documents_fail = documents_fail
        self.documents_malformed = documents_malformed
        self.comments_fail = comments_fail
        self.comments_malformed = comments_malformed
        self.comments_nonzero = comments_nonzero
        self.summary_mismatch = summary_mismatch
        self.leeds_unexpected_detail = leeds_unexpected_detail
        self.search_type = search_type
        self.uncounted_terminal = uncounted_terminal
        self.uncounted_label = uncounted_label
        self.showing_counts = showing_counts
        self.validated_showing_markers = validated_showing_markers
        self.validated_legacy_count = validated_legacy_count
        self.expected_week = expected_week
        self.validated_current_page = validated_current_page
        self.west_suffolk_open_fault = west_suffolk_open_fault
        self.current_date_type = ""
        self.current_open_query: tuple[str, str] | None = None
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.attachment_paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        path = request.url.path
        action = request.url.params.get("action")
        if path.endswith("/search.do") and action == "weeklyList":
            return httpx.Response(
                200,
                headers={"set-cookie": "JSESSIONID=sanitised; Path=/"},
                content=_weekly_form(self.search_type),
            )
        if path.endswith("/search.do") and action == "advanced":
            assert self.case.authority_id == AuthorityId("west-suffolk")
            return httpx.Response(
                200,
                content=_advanced_form(
                    malformed=self.west_suffolk_open_fault == "malformed-form",
                    missing_option=self.west_suffolk_open_fault == "missing-option",
                    missing_form=self.west_suffolk_open_fault == "missing-form",
                    wrong_action=self.west_suffolk_open_fault == "wrong-action",
                    wrong_method=self.west_suffolk_open_fault == "wrong-method",
                    missing_hidden=self.west_suffolk_open_fault == "missing-hidden",
                ),
            )
        if path.endswith("/weeklyListResults.do"):
            assert request.headers.get("cookie") == "JSESSIONID=sanitised"
            assert fields[:5] == (
                ("_csrf", "sanitised-token"),
                ("searchCriteria.parish", ""),
                ("searchCriteria.ward", ""),
                ("week", self.expected_week),
                ("dateType", dict(fields)["dateType"]),
            )
            assert fields[5][0] == "searchType"
            assert fields[-2:] == (("tag", "one"), ("tag", "two"))
            self.current_date_type = dict(fields)["dateType"]
            if self.uncounted_terminal and self.current_date_type == "DC_Validated":
                return httpx.Response(
                    200,
                    content=_uncounted_result_page(
                        tuple(
                            zip(
                                self.case.references[:3],
                                self.case.locators[:3],
                                strict=True,
                            )
                        ),
                        label=self.uncounted_label,
                    ),
                )
            if self.current_date_type == "DC_Decided":
                return httpx.Response(
                    200,
                    content=_result_page(
                        (
                            (self.case.references[0], self.case.locators[0]),
                            (self.case.references[3], self.case.locators[3]),
                        ),
                        count=2,
                        count_text=(
                            "Showing 1\N{EN DASH}2 of 2 results"
                            if self.showing_counts
                            else None
                        ),
                    ),
                )
            validated_rows = (
                (self.case.references[0], self.case.locators[0]),
                (self.case.references[1], self.case.locators[1]),
            )
            if self.validated_showing_markers is not None:
                return httpx.Response(
                    200,
                    content=_result_page_with_showing_markers(
                        validated_rows,
                        self.validated_showing_markers,
                        current_page=self.validated_current_page,
                    ),
                )
            if self.validated_legacy_count is not None:
                return httpx.Response(
                    200,
                    content=_result_page_with_legacy_count(
                        validated_rows,
                        self.validated_legacy_count,
                    ),
                )
            if self.showing_counts:
                return httpx.Response(
                    200,
                    content=_result_page_with_showing_markers(
                        validated_rows,
                        ("Showing 1-2 of 3", "Showing 1-2 of 3"),
                    ),
                )
            return httpx.Response(
                200,
                content=_result_page(
                    validated_rows,
                    count=1 if self.mismatch else 3,
                    count_text=("Showing 1-2 of 3" if self.showing_counts else None),
                ),
            )
        if path.endswith("/advancedSearchResults.do"):
            assert self.case.authority_id == AuthorityId("west-suffolk")
            values = dict(fields)
            active = tuple(
                (field, values[field])
                for field in (
                    "searchCriteria.caseStatus",
                    "searchCriteria.appealStatus",
                )
                if values[field]
            )
            assert len(active) == 1
            self.current_date_type = ""
            self.current_open_query = active[0]
            return httpx.Response(
                200,
                content=_west_suffolk_open_page(
                    self.current_open_query,
                    1,
                    self.west_suffolk_open_fault,
                ),
            )
        if path.endswith("/pagedSearchResults.do"):
            if self.current_open_query is not None:
                return httpx.Response(
                    200,
                    content=_west_suffolk_open_page(
                        self.current_open_query,
                        2,
                        self.west_suffolk_open_fault,
                    ),
                )
            assert self.current_date_type == "DC_Validated"
            rows = (
                ()
                if self.empty_stall
                else ((self.case.references[2], self.case.locators[2]),)
            )
            content = (
                _result_page_with_showing_markers(
                    rows,
                    ("Showing 3-3 of 3 result", "Showing 3-3 of 3 result"),
                    current_page="2",
                    numbered_page=1,
                )
                if self.showing_counts
                else _result_page(rows, count=3)
            )
            return httpx.Response(200, content=content)
        if path.endswith("/applicationDetails.do"):
            locator = request.url.params["keyVal"]
            known = dict(zip(self.case.locators, self.case.references, strict=True)) | {
                open_locator: open_reference
                for open_reference, open_locator in _WEST_SUFFOLK_OPEN_REFERENCES
            }
            reference = known[locator]
            tab = request.url.params["activeTab"]
            if tab == "summary":
                if self.case.authority_id == AuthorityId("leeds"):
                    body = (
                        b"<p>Unexpected detail response</p>"
                        if self.leeds_unexpected_detail
                        else (
                            b"<p>Unable to perform this task. "
                            b"A remote exception occurred.</p>"
                        )
                    )
                    return httpx.Response(200, content=body)
                return httpx.Response(
                    200,
                    content=_summary(
                        "MISMATCH/0001" if self.summary_mismatch else reference,
                        self.case.authority_id,
                    ),
                )
            if tab == "documents":
                if self.documents_fail:
                    return httpx.Response(404)
                if self.documents_malformed:
                    return httpx.Response(
                        200,
                        content=(
                            b'<h2 data-section="documents" data-count="1">'
                            b"Documents (1)</h2>"
                        ),
                    )
                return httpx.Response(200, content=DOCUMENTS)
            if tab == "neighbourComments":
                assert self.case.authority_id == AuthorityId("cornwall")
                if self.comments_fail:
                    return httpx.Response(404)
                if self.comments_malformed:
                    return httpx.Response(200, content=b"<p>Unknown</p>")
                return httpx.Response(
                    200,
                    content=(
                        b'<h2 data-section="comments" data-count="'
                        + (b"1" if self.comments_nonzero else b"0")
                        + b'">Comments</h2>'
                    ),
                )
            if tab == "consulteeComments":
                assert self.case.authority_id == AuthorityId("durham")
                if self.comments_fail:
                    return httpx.Response(404)
                if self.comments_malformed:
                    return httpx.Response(200, content=b"<p>Unknown</p>")
                return httpx.Response(
                    200,
                    content=(
                        b'<h2 data-section="consultee comments" data-count="'
                        + (b"1" if self.comments_nonzero else b"0")
                        + b'">'
                        b"Consultee Comments (0)</h2>"
                    ),
                )
        if path.endswith((".pdf", ".tif", ".doc", ".zip")):
            self.attachment_paths.append(path)
            return httpx.Response(500)
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def _session(mock: _IdoxMock) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )


class _QualificationSession(HttpxPortalSession):
    def __init__(self, mock: _IdoxMock) -> None:
        super().__init__(
            client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
            limiter=HostRateLimiter(0),
            max_attempts=1,
        )
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class _PortalRequestSpy(HttpxPortalSession):
    def __init__(self, mock: _IdoxMock) -> None:
        super().__init__(
            client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
            limiter=HostRateLimiter(0),
            max_attempts=1,
        )
        self.requests: list[PortalRequest] = []

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        return await super().fetch(request)


def _store(root: Path) -> SqliteStore:
    return SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_west_suffolk.py"
    name = "_test_qualify_west_suffolk"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize("search_type", ["Application", "PortalOwnedSentinel"])
def test_weekly_request_preserves_authoritative_hidden_form_fields(
    case: _Case,
    search_type: str,
) -> None:
    """Treat form-owned search discriminators as opaque portal state."""
    mock = _IdoxMock(case, search_type=search_type)
    session = _PortalRequestSpy(mock)
    package = pilot_registry().get(case.authority_id)

    async def discover_first_page() -> None:
        batches = cast(
            "AsyncGenerator[DurableDiscoveryBatch]",
            package.discover(session, WEEK, None),
        )
        await anext(batches)
        await batches.aclose()
        await session.aclose()

    asyncio.run(discover_first_page())
    request = next(request for request in session.requests if request.form)

    assert tuple((field.name, field.value) for field in request.form) == (
        ("_csrf", "sanitised-token"),
        ("searchCriteria.parish", ""),
        ("searchCriteria.ward", ""),
        ("week", "14/09/2026"),
        ("dateType", "DC_Validated"),
        ("searchType", search_type),
        ("tag", "one"),
        ("tag", "two"),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize("label", ["Reference", "Ref. No"])
def test_authority_accepts_live_shaped_uncounted_terminal_page(
    case: _Case,
    label: str,
) -> None:
    """A non-empty under-capacity first page without pagination is complete."""
    mock = _IdoxMock(case, uncounted_terminal=True, uncounted_label=label)
    session = _session(mock)
    package = pilot_registry().get(case.authority_id)

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [batch async for batch in package.discover(session, WEEK, None)]
        await session.aclose()
        return batches

    batch = asyncio.run(discover_all())[0]
    assert [reference.reference for reference in batch.references] == list(
        case.references[:3]
    )
    assert [reference.locator for reference in batch.references] == list(
        case.locators[:3]
    )
    assert not any(
        path.endswith("/pagedSearchResults.do") for _, path, _ in mock.requests
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_weekly_discovery_resumes_deduplicates_and_counts(
    case: _Case,
) -> None:
    """Every authority owns a resumable validated-plus-decided weekly flow."""
    package = pilot_registry().get(case.authority_id)
    first_mock = _IdoxMock(case)
    first_session = _session(first_mock)

    async def first_page() -> DurableDiscoveryBatch:
        batches = cast(
            "AsyncGenerator[DurableDiscoveryBatch]",
            package.discover(first_session, WEEK, None),
        )
        batch = await anext(batches)
        await batches.aclose()
        await first_session.aclose()
        return batch

    first = asyncio.run(first_page())
    assert [reference.reference for reference in first.references] == list(
        case.references[:2]
    )
    assert [reference.locator for reference in first.references] == list(
        case.locators[:2]
    )

    resumed_mock = _IdoxMock(case)
    resumed_session = _session(resumed_mock)

    async def resume() -> list[DurableDiscoveryBatch]:
        batches = [
            batch
            async for batch in package.discover(
                resumed_session,
                WEEK,
                first.next_checkpoint,
            )
        ]
        await resumed_session.aclose()
        return batches

    resumed = asyncio.run(resume())
    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == [case.references[2], case.references[3]]
    assert resumed[-1].complete
    posts = [request for request in resumed_mock.requests if request[0] == "POST"]
    assert [dict(request[2])["dateType"] for request in posts] == [
        "DC_Validated",
        "DC_Decided",
    ]
    assert (
        sum(
            path.endswith("/pagedSearchResults.do")
            for _, path, _ in resumed_mock.requests
        )
        == 1
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_showing_totals_drive_public_pagination(case: _Case) -> None:
    """Displayed Showing totals drive one bounded page request to completeness."""
    mock = _IdoxMock(case, showing_counts=True)
    session = _session(mock)
    package = pilot_registry().get(case.authority_id)

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [batch async for batch in package.discover(session, WEEK, None)]
        await session.aclose()
        return batches

    batches = asyncio.run(discover_all())
    assert [
        reference.reference for batch in batches for reference in batch.references
    ] == list(case.references)
    assert batches[-1].complete
    assert (
        sum(path.endswith("/pagedSearchResults.do") for _, path, _ in mock.requests)
        == 1
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize(
    "showing_markers",
    [
        ("Showing 1-2 of 2 bedrooms",),
        ("Showing 1-2 of 2", "Showing 1-2 of 3"),
        ("Showing 1-10 of 2",),
        ("Showing 1-2 of 2", "Showing 1-1 of 2"),
        ("Showing 1-1 of 2",),
        ("Showing 1-2 of 2",),
    ],
    ids=[
        "trailing-junk",
        "conflicting-totals",
        "impossible-range",
        "conflicting-ranges",
        "span-row-mismatch",
        "terminal-forward-page",
    ],
)
def test_authority_public_discovery_rejects_ambiguous_showing_markers(
    case: _Case,
    showing_markers: tuple[str, ...],
) -> None:
    """Ambiguous displayed ranges cannot truncate pagination."""
    mock = _IdoxMock(case, validated_showing_markers=showing_markers)
    session = _session(mock)
    package = pilot_registry().get(case.authority_id)
    parse_error = _member(case, "ParseError")

    async def discover_all() -> None:
        with pytest.raises(parse_error, match="reported result count"):
            async for _batch in package.discover(session, WEEK, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())
    assert not any(
        path.endswith("/pagedSearchResults.do") for _, path, _ in mock.requests
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize(
    "legacy_count",
    ["Total 2 bedrooms", "Displaying 1 of 2 bedrooms"],
    ids=["total-trailing-text", "displaying-trailing-text"],
)
def test_authority_public_discovery_rejects_ambiguous_legacy_totals(
    case: _Case,
    legacy_count: str,
) -> None:
    """Unrelated trailing text cannot turn legacy prose into a result count."""
    mock = _IdoxMock(case, validated_legacy_count=legacy_count)
    session = _session(mock)
    package = pilot_registry().get(case.authority_id)
    parse_error = _member(case, "ParseError")

    async def discover_all() -> None:
        with pytest.raises(parse_error, match="reported result count"):
            async for _batch in package.discover(session, WEEK, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())
    assert not any(
        path.endswith("/pagedSearchResults.do") for _, path, _ in mock.requests
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_weekly_discovery_rejects_mismatch_and_open_scope(
    case: _Case,
) -> None:
    """Displayed count disagreement and older-open scope are never completeness."""
    package = pilot_registry().get(case.authority_id)

    async def mismatch() -> None:
        session = _session(_IdoxMock(case, mismatch=True))
        with pytest.raises(ValueError, match="expected 1 actual 2"):
            async for _batch in package.discover(session, WEEK, None):
                pass
        await session.aclose()

    asyncio.run(mismatch())

    if case.authority_id == AuthorityId("west-suffolk"):
        return

    async def open_scope() -> None:
        session = _session(_IdoxMock(case))
        with pytest.raises(RuntimeError, match="older-open"):
            async for _batch in package.discover(
                session,
                WEEK.model_copy(update={"include_open": True}),
                None,
            ):
                pass
        await session.aclose()

    asyncio.run(open_scope())


def test_west_suffolk_open_discovery_exhausts_all_active_partitions() -> None:
    """Open discovery preserves the form and completes every proven partition."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    mock = _IdoxMock(_WEST_SUFFOLK_CASE)
    session = _PortalRequestSpy(mock)
    window = WEEK.model_copy(update={"include_open": True})

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [batch async for batch in package.discover(session, window, None)]
        await session.aclose()
        return batches

    batches = asyncio.run(discover_all())
    assert [
        reference.reference for batch in batches for reference in batch.references
    ] == [
        "DC/26/1388/TCA",
        "DC/26/1389/FUL",
        "DC/26/1390/FUL",
        "DC/26/1391/FUL",
        "DC/26/2001/FUL",
        "DC/26/2002/FUL",
        "DC/26/2003/FUL",
        "DC/26/2004/FUL",
        "DC/26/2005/FUL",
        "DC/26/2006/FUL",
        "DC/26/2007/FUL",
        "DC/26/2008/FUL",
    ]
    assert batches[-1].complete
    advanced_posts = [
        request.form
        for request in session.requests
        if request.method.value == "POST"
        and str(request.url.path).endswith("/advancedSearchResults.do")
    ]
    expected_queries = [
        ("searchCriteria.caseStatus", "Pending Consideration"),
        ("searchCriteria.caseStatus", "Pending Decision"),
        ("searchCriteria.caseStatus", "Received Awaiting Registration"),
        ("searchCriteria.caseStatus", "Pending Appeal Decision"),
        ("searchCriteria.appealStatus", "Appeal lodged"),
        (
            "searchCriteria.appealStatus",
            "Appeal Remitted to Secretary of State",
        ),
        ("searchCriteria.appealStatus", "High Court Appeal Lodged"),
        ("searchCriteria.appealStatus", "Pending Appeal Decision"),
    ]
    assert [
        tuple((field.name, field.value) for field in form) for form in advanced_posts
    ] == [_expected_advanced_fields(field, value) for field, value in expected_queries]
    assert mock.attachment_paths == []
    assert session.attachment_body_requests == 0


def test_west_suffolk_open_discovery_resumes_without_duplicate_references() -> None:
    """A resumed advanced page restores its search and emits only unseen records."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    window = WEEK.model_copy(update={"include_open": True})
    first_mock = _IdoxMock(_WEST_SUFFOLK_CASE)
    first_session = _session(first_mock)

    async def stop_after_first_open_page() -> tuple[list[str], StoredCheckpoint]:
        references: list[str] = []
        discovery = cast(
            "AsyncGenerator[DurableDiscoveryBatch]",
            package.discover(first_session, window, None),
        )
        try:
            async for batch in discovery:
                references.extend(reference.reference for reference in batch.references)
                checkpoint = json.loads(batch.next_checkpoint.payload_json)
                if checkpoint["active_query"] == (
                    "advanced|searchCriteria.caseStatus|Pending Consideration"
                ):
                    return references, batch.next_checkpoint
        finally:
            await discovery.aclose()
            await first_session.aclose()
        message = "advanced discovery did not reach page two"
        raise AssertionError(message)

    first_references, checkpoint = asyncio.run(stop_after_first_open_page())
    assert first_references == [
        "DC/26/1388/TCA",
        "DC/26/1389/FUL",
        "DC/26/1390/FUL",
        "DC/26/1391/FUL",
        "DC/26/2001/FUL",
    ]

    resumed_mock = _IdoxMock(_WEST_SUFFOLK_CASE)
    resumed_session = _session(resumed_mock)

    async def resume() -> list[DurableDiscoveryBatch]:
        batches = [
            batch
            async for batch in package.discover(resumed_session, window, checkpoint)
        ]
        await resumed_session.aclose()
        return batches

    resumed = asyncio.run(resume())
    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == [
        "DC/26/2002/FUL",
        "DC/26/2003/FUL",
        "DC/26/2004/FUL",
        "DC/26/2005/FUL",
        "DC/26/2006/FUL",
        "DC/26/2007/FUL",
        "DC/26/2008/FUL",
    ]
    assert resumed[-1].complete
    advanced_requests = [
        (method, path)
        for method, path, _fields in resumed_mock.requests
        if path.endswith(("/advancedSearchResults.do", "/pagedSearchResults.do"))
    ]
    assert advanced_requests[:2] == [
        ("POST", "/online-applications/advancedSearchResults.do"),
        ("GET", "/online-applications/pagedSearchResults.do"),
    ]

    terminal_session = _session(_IdoxMock(_WEST_SUFFOLK_CASE))

    async def repeat_terminal() -> list[DurableDiscoveryBatch]:
        batches = [
            batch
            async for batch in package.discover(
                terminal_session,
                window,
                resumed[-1].next_checkpoint,
            )
        ]
        await terminal_session.aclose()
        return batches

    terminal = asyncio.run(repeat_terminal())
    assert terminal == [
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=resumed[-1].next_checkpoint,
            complete=True,
        )
    ]
    assert terminal_session.requested_urls == ()


def test_west_suffolk_open_discovery_recovers_completed_progress() -> None:
    """Completed partitions converge when a prior crash missed the terminal flag."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    window = WEEK.model_copy(update={"include_open": True})
    checkpoint = west_suffolk_adapter.WestSuffolkCheckpointV1(
        result_page="live",
        live_scope=west_suffolk_adapter.WestSuffolkDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=True,
        ),
        completed_queries=(
            "14/09/2026|DC_Validated",
            "14/09/2026|DC_Decided",
            "advanced|searchCriteria.caseStatus|Pending Consideration",
            "advanced|searchCriteria.caseStatus|Pending Decision",
            "advanced|searchCriteria.caseStatus|Received Awaiting Registration",
            "advanced|searchCriteria.caseStatus|Pending Appeal Decision",
            "advanced|searchCriteria.appealStatus|Appeal lodged",
            "advanced|searchCriteria.appealStatus|Appeal Remitted to Secretary of State",
            "advanced|searchCriteria.appealStatus|High Court Appeal Lodged",
            "advanced|searchCriteria.appealStatus|Pending Appeal Decision",
        ),
    )
    session = _session(_IdoxMock(_WEST_SUFFOLK_CASE))

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [
            batch
            async for batch in package.discover(
                session,
                window,
                StoredCheckpoint(
                    schema_version=1,
                    payload_json=checkpoint.model_dump_json(),
                ),
            )
        ]
        await session.aclose()
        return batches

    batches = asyncio.run(discover_all())
    assert len(batches) == 1
    assert batches[0].references == ()
    assert batches[0].complete


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("malformed-form", "advanced form"),
        ("missing-form", "advanced form"),
        ("wrong-action", "advanced form"),
        ("wrong-method", "advanced form"),
        ("missing-hidden", "advanced form"),
        ("missing-option", "advanced form status options"),
        ("ambiguous-detail", "advanced detail"),
        ("multiple-detail-tables", "advanced detail"),
        ("mixed-detail", "advanced detail"),
        ("empty-detail", "advanced detail"),
        ("invalid-detail-link", "advanced detail keyVal"),
        ("detail-on-page-two", "advanced detail"),
        ("count-mismatch", "expected 1 actual 2"),
        ("stalled-pagination", "expected 3 actual 2"),
    ],
)
def test_west_suffolk_open_discovery_fails_closed(
    fault: str,
    message: str,
) -> None:
    """Malformed advanced boundaries never become complete discovery."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    session = _session(_IdoxMock(_WEST_SUFFOLK_CASE, west_suffolk_open_fault=fault))
    window = WEEK.model_copy(update={"include_open": True})

    async def discover_all() -> None:
        with pytest.raises(ValueError, match=message):
            async for _batch in package.discover(session, window, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())


def test_west_suffolk_open_discovery_rejects_unknown_checkpoint_query() -> None:
    """Only the current weekly and advanced query inventory can resume."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    window = WEEK.model_copy(update={"include_open": True})
    checkpoint = west_suffolk_adapter.WestSuffolkCheckpointV1(
        result_page="live",
        live_scope=west_suffolk_adapter.WestSuffolkDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=True,
        ),
        active_query="advanced|searchCriteria.caseStatus|Unknown",
    )
    session = _session(_IdoxMock(_WEST_SUFFOLK_CASE))

    async def discover_all() -> None:
        with pytest.raises(ValueError, match="checkpoint query"):
            async for _batch in package.discover(
                session,
                window,
                StoredCheckpoint(
                    schema_version=1,
                    payload_json=checkpoint.model_dump_json(),
                ),
            ):
                pass
        await session.aclose()

    asyncio.run(discover_all())


def test_west_suffolk_weekly_count_rejects_empty_page_marker() -> None:
    """The advanced empty first-page marker cannot weaken weekly parsing."""
    package = pilot_registry().get(AuthorityId("west-suffolk"))
    session = _session(
        _IdoxMock(
            _WEST_SUFFOLK_CASE,
            validated_showing_markers=("Showing 1-2 of 3",),
            validated_current_page="",
        )
    )

    async def discover_all() -> None:
        with pytest.raises(
            west_suffolk_adapter.WestSuffolkParseError,
            match="reported result count",
        ):
            async for _batch in package.discover(session, WEEK, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())


def test_west_suffolk_count_accepts_stale_hidden_marker_on_terminal_page() -> None:
    """The visible range outranks IDOX's stale hidden first-page marker."""
    rows = tuple((f"DC/26/{index:04d}/FUL", f"KEY{index}") for index in range(5))
    page = _result_page_with_showing_markers(
        rows,
        ("Showing 41-45 of 45", "Showing 41-45 of 45"),
        current_page="1",
        numbered_page=4,
        visible_page="5",
    )

    parse_search_page = getattr(west_suffolk_adapter, "_parse_search_page")
    parsed = parse_search_page(page)

    assert parsed.reported == 45
    assert len(parsed.references) == 5


@pytest.mark.parametrize(
    "visible_pages",
    [("1", "2"), ("later", "later"), ("0", "0")],
    ids=["conflicting", "non-numeric", "zero"],
)
def test_west_suffolk_count_rejects_invalid_visible_page_markers(
    visible_pages: tuple[str, str],
) -> None:
    """Visible current-page markers must be positive and agree."""
    rows = (("DC/26/0001/FUL", "KEY1"),)
    markers = "".join(
        '<p class="pager"><span class="showing">Showing 1-1 of 1</span>'
        f"<strong>{page}</strong></p>"
        for page in visible_pages
    )
    body = markers.encode() + _uncounted_result_page(rows)

    with pytest.raises(
        west_suffolk_adapter.WestSuffolkParseError,
        match="reported result count",
    ):
        getattr(west_suffolk_adapter, "_parse_search_page")(body)


def test_west_suffolk_count_rejects_visible_page_outside_displayed_range() -> None:
    """A false visible page number cannot conceal a forward result link."""
    page = _result_page_with_showing_markers(
        (("DC/26/0001/FUL", "KEY1"),),
        ("Showing 1-1 of 1", "Showing 1-1 of 1"),
        current_page="1",
        numbered_page=2,
        visible_page="999",
    )

    with pytest.raises(
        west_suffolk_adapter.WestSuffolkParseError,
        match="reported result count",
    ):
        getattr(west_suffolk_adapter, "_parse_search_page")(page)


@pytest.mark.parametrize("capacity", [None, "many", "0"])
def test_west_suffolk_count_requires_capacity_for_visible_page(
    capacity: str | None,
) -> None:
    """A visible page number needs a positive selected page capacity."""
    page = _result_page_with_showing_markers(
        (("DC/26/0001/FUL", "KEY1"),),
        ("Showing 1-1 of 1", "Showing 1-1 of 1"),
        visible_page="1",
        numbered_page=1,
        capacity=capacity,
    )

    with pytest.raises(
        west_suffolk_adapter.WestSuffolkParseError,
        match="reported result count",
    ):
        getattr(west_suffolk_adapter, "_parse_search_page")(page)


async def _collect_live_case(
    case: _Case,
    root: Path,
) -> tuple[SqliteStore, ApplicationId]:
    store = _store(root)
    collector = Collector(pilot_registry(), store)
    mock = _IdoxMock(case)
    session = _session(mock)
    report = await collector.collect(case.authority_id, WEEK, session)
    await session.aclose()
    assert len(report.applications) == 4
    assert report.attachment_body_requests == 0
    assert mock.attachment_paths == []
    application_id = report.applications[0]
    application = store.get_application(application_id)
    assert len(application.documents) == 2
    assert application.completeness.documents.kind == "complete"
    assert application.completeness.comments.kind in {"empty", "unavailable"}
    view = store.application_view(application_id)
    assert view.metadata.aliases == ("PP-15200000",)
    assert view.metadata.address == "1 Sanitised Street"
    assert view.metadata.validated_date == date(2026, 9, 14)
    assert str(view.metadata.source_url) == f"{case.base_url}/applicationDetails.do"
    assert store.discovery_state(case.authority_id).queued[0].locator is not None
    assert store.retained_native_records()[0].reference.locator

    repeat_mock = _IdoxMock(case)
    repeat_session = _session(repeat_mock)
    await collector.collect(case.authority_id, WEEK, repeat_session)
    await repeat_session.aclose()
    assert store.semantic_version_count(application_id, "application") == 1
    assert store.semantic_version_count(application_id, "documents") == 1

    failing_probe = _session(_IdoxMock(case, documents_fail=True))
    failed_observation = (
        await pilot_registry()
        .get(case.authority_id)
        .collect(
            failing_probe,
            store.discovery_state(case.authority_id).queued[0],
        )
    )
    await failing_probe.aclose()
    assert failed_observation.normalised.completeness.documents.kind == "failed"

    failing_mock = _IdoxMock(case, documents_fail=True)
    failing_session = _session(failing_mock)
    await collector.collect(case.authority_id, WEEK, failing_session)
    await failing_session.aclose()
    preserved = store.get_application(application_id)
    assert len(preserved.documents) == 2
    assert preserved.completeness.documents.kind == "complete"
    assert store.semantic_version_count(application_id, "documents") == 1
    assert failing_mock.attachment_paths == []
    return store, application_id


def test_cornwall_live_collection_uses_zero_comment_evidence(tmp_path: Path) -> None:
    """Cornwall records an empty comment section only from its displayed zero."""
    store, application_id = asyncio.run(_collect_live_case(CASES[0], tmp_path))
    assert store.get_application(application_id).completeness.comments.kind == "empty"
    store.close()


def test_durham_live_collection_keeps_public_comments_unavailable(
    tmp_path: Path,
) -> None:
    """A zero consultee count does not make Durham public comments empty."""
    store, application_id = asyncio.run(_collect_live_case(CASES[1], tmp_path))
    assert store.get_application(application_id).completeness.comments.kind == (
        "unavailable"
    )
    retained = store.retained_native_records()[0]
    assert json.loads(retained.native_json)["consultee_comment_count"] == 0
    store.close()


def test_west_suffolk_live_collection_never_opens_representations(
    tmp_path: Path,
) -> None:
    """West Suffolk representation attachments remain metadata-only documents."""
    store, application_id = asyncio.run(_collect_live_case(CASES[2], tmp_path))
    assert store.get_application(application_id).completeness.comments.kind == (
        "unavailable"
    )
    store.close()


def test_leeds_detail_boundary_preserves_remote_and_unverified_failures() -> None:
    """Leeds discovery succeeds while detail remains an explicit source boundary."""
    case = CASES[3]
    package = pilot_registry().get(case.authority_id)
    reference = SourceReference(
        source_id=case.source_id,
        reference=case.references[0],
        locator=case.locators[0],
    )

    async def exercise() -> None:
        remote_session = _session(_IdoxMock(case))
        with pytest.raises(
            leeds_adapter.LeedsDetailUnavailableError,
            match="remote exception",
        ):
            await package.collect(remote_session, reference)
        await remote_session.aclose()

        unexpected_session = _session(_IdoxMock(case, leeds_unexpected_detail=True))
        with pytest.raises(
            leeds_adapter.LeedsDetailUnverifiedError,
            match="not verified",
        ):
            await package.collect(unexpected_session, reference)
        await unexpected_session.aclose()

        missing_session = _session(_IdoxMock(case))
        with pytest.raises(leeds_adapter.LeedsRoutingError, match=case.references[0]):
            await package.collect(
                missing_session,
                reference.model_copy(update={"locator": None}),
            )
        await missing_session.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_completed_checkpoint_is_bound_to_live_window(
    case: _Case,
) -> None:
    """A completed window cannot suppress discovery for a later window."""
    package = pilot_registry().get(case.authority_id)

    async def exercise() -> None:
        initial_session = _session(_IdoxMock(case))
        initial = [
            batch async for batch in package.discover(initial_session, WEEK, None)
        ]
        await initial_session.aclose()
        checkpoint = initial[-1].next_checkpoint
        assert initial[-1].complete

        repeated_session = _session(_IdoxMock(case))
        repeated = [
            batch
            async for batch in package.discover(repeated_session, WEEK, checkpoint)
        ]
        assert repeated_session.requested_urls == ()
        assert repeated == [
            DurableDiscoveryBatch(
                references=(),
                next_checkpoint=checkpoint,
                complete=True,
            )
        ]
        await repeated_session.aclose()

        later_mock = _IdoxMock(case, expected_week="21/09/2026")
        later_session = _session(later_mock)
        later = [
            batch
            async for batch in package.discover(
                later_session,
                LATER_WEEK,
                checkpoint,
            )
        ]
        await later_session.aclose()
        assert [
            reference.reference for batch in later for reference in batch.references
        ] == list(case.references)
        assert [
            dict(fields)["week"]
            for method, path, fields in later_mock.requests
            if method == "POST" and path.endswith("/weeklyListResults.do")
        ] == ["21/09/2026", "21/09/2026"]

    asyncio.run(exercise())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_legacy_unscoped_live_checkpoint_restarts(case: _Case) -> None:
    """Legacy live completion cannot be reused for an unknown window."""
    package = pilot_registry().get(case.authority_id)
    checkpoint_type = _member(case, "CheckpointV1")
    legacy = StoredCheckpoint(
        schema_version=1,
        payload_json=checkpoint_type(
            result_page="live",
            live_complete=True,
        ).model_dump_json(exclude_none=True),
    )

    async def exercise() -> None:
        mock = _IdoxMock(case)
        session = _session(mock)
        batches = [batch async for batch in package.discover(session, WEEK, legacy)]
        await session.aclose()
        assert [
            reference.reference for batch in batches for reference in batch.references
        ] == list(case.references)
        assert mock.requests

    asyncio.run(exercise())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_stale_active_query_does_not_cross_live_window(
    case: _Case,
) -> None:
    """Partial progress from another window is discarded before validation."""
    package = pilot_registry().get(case.authority_id)

    async def exercise() -> None:
        initial_session = _session(_IdoxMock(case))
        initial = [
            batch async for batch in package.discover(initial_session, WEEK, None)
        ]
        await initial_session.aclose()
        payload = json.loads(initial[-1].next_checkpoint.payload_json)
        payload.update(
            {
                "active_query": "14/09/2026|DC_Validated",
                "live_complete": False,
                "next_page": 2,
                "query_row_count": 2,
                "seen_references": [case.references[0]],
            }
        )
        stale = StoredCheckpoint(
            schema_version=1,
            payload_json=json.dumps(payload, separators=(",", ":")),
        )

        later_session = _session(_IdoxMock(case, expected_week="21/09/2026"))
        later = [
            batch async for batch in package.discover(later_session, LATER_WEEK, stale)
        ]
        await later_session.aclose()
        assert [
            reference.reference for batch in later for reference in batch.references
        ] == list(case.references)

    asyncio.run(exercise())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_checkpoint_and_empty_window_boundaries(case: _Case) -> None:
    """Terminal, stale, empty-window, and stalled checkpoints stay explicit."""
    adapter = _member(case, "Adapter")()
    checkpoint_type = _member(case, "CheckpointV1")
    scope_type = _member(case, "DiscoveryScope")

    async def exercise() -> None:
        terminal_session = _session(_IdoxMock(case))
        scope = scope_type(
            start=WEEK.start,
            end=WEEK.end,
            include_open=WEEK.include_open,
        )
        terminal_checkpoint = checkpoint_type(
            result_page="live",
            live_scope=scope,
            live_complete=True,
        )
        terminal = [
            batch
            async for batch in adapter.discover(
                terminal_session,
                WEEK,
                terminal_checkpoint,
            )
        ]
        assert len(terminal) == 1
        assert terminal[0].complete
        assert terminal_session.requested_urls == ()
        await terminal_session.aclose()

        terminal_open_session = _session(_IdoxMock(case))
        open_window = WEEK.model_copy(update={"include_open": True})
        terminal_open_checkpoint = checkpoint_type(
            result_page="live",
            live_scope=scope_type(
                start=open_window.start,
                end=open_window.end,
                include_open=open_window.include_open,
            ),
            live_complete=True,
        )
        if case.authority_id == AuthorityId("west-suffolk"):
            terminal_open = [
                batch
                async for batch in adapter.discover(
                    terminal_open_session,
                    open_window,
                    terminal_open_checkpoint,
                )
            ]
            assert terminal_open[0].complete
            assert terminal_open_session.requested_urls == ()
        else:
            with pytest.raises(RuntimeError, match="older-open"):
                async for _batch in adapter.discover(
                    terminal_open_session,
                    open_window,
                    terminal_open_checkpoint,
                ):
                    pass
        await terminal_open_session.aclose()

        stale_session = _session(_IdoxMock(case))
        stale = checkpoint_type(
            result_page="live",
            live_scope=scope,
            active_query="01/01/2000|DC_Validated",
        )
        with pytest.raises(ValueError, match="checkpoint query"):
            async for _batch in adapter.discover(stale_session, WEEK, stale):
                pass
        await stale_session.aclose()

        outside = DiscoveryWindow(
            start=date(2027, 1, 4),
            end=date(2027, 1, 10),
            include_open=False,
        )
        empty_session = _session(_IdoxMock(case))
        empty = [
            batch async for batch in adapter.discover(empty_session, outside, None)
        ]
        assert len(empty) == 1
        assert empty[0].complete
        assert empty[0].references == ()
        await empty_session.aclose()

        stalled_session = _session(_IdoxMock(case, empty_stall=True))
        with pytest.raises(ValueError, match="expected 3 actual 2"):
            async for _batch in adapter.discover(stalled_session, WEEK, None):
                pass
        await stalled_session.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize("label", ["Reference", "Ref. No", "Ref No"])
def test_authority_search_result_reference_labels(case: _Case, label: str) -> None:
    """Recorded IDOX reference labels map to one source reference field."""
    parsed = getattr(case.module, "_parse_search_page")(
        _result_page(
            ((case.references[0], case.locators[0]),),
            count=1,
            label=label,
        )
    )
    assert parsed.references[0].reference == case.references[0]


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize(
    ("separator", "suffix"),
    [("-", ""), ("-", " result"), ("\N{EN DASH}", " results")],
)
def test_authority_accepts_explicit_showing_result_total(
    case: _Case,
    separator: str,
    suffix: str,
) -> None:
    """A displayed Showing range remains the authority for paginated totals."""
    parsed = getattr(case.module, "_parse_search_page")(
        _paginated_result_page(
            _ten_result_rows(case),
            f"Showing 1{separator}10 of 14{suffix}",
        )
    )
    assert len(parsed.references) == 10
    assert parsed.reported == 14


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize(
    "count_text",
    [
        "Showing one-10 of 14",
        "Showing 1 to 10 of 14",
        "Showing 1-10 of fourteen",
        "Showing 1-10 from 14",
    ],
)
def test_authority_rejects_malformed_showing_result_total(
    case: _Case,
    count_text: str,
) -> None:
    """Malformed Showing text cannot bypass pagination completeness checks."""
    parse_error = _member(case, "ParseError")
    with pytest.raises(parse_error, match="reported result count"):
        getattr(case.module, "_parse_search_page")(
            _paginated_result_page(_ten_result_rows(case), count_text)
        )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_does_not_treat_malformed_showing_as_uncounted(
    case: _Case,
) -> None:
    """A malformed displayed count cannot become an inferred terminal total."""
    parse_error = _member(case, "ParseError")
    body = b'<span class="showing">Showing 1 to 1 of 1</span>' + _uncounted_result_page(
        ((case.references[0], case.locators[0]),)
    )
    with pytest.raises(parse_error, match="reported result count"):
        getattr(case.module, "_parse_search_page")(body)


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
@pytest.mark.parametrize(
    "shape",
    [
        _UncountedPageShape("empty", ()),
        _UncountedPageShape("full-capacity", (("A", "KEY"),), capacity="1"),
        _UncountedPageShape("missing-capacity", (("A", "KEY"),), capacity=None),
        _UncountedPageShape("invalid-capacity", (("A", "KEY"),), capacity="many"),
        _UncountedPageShape("zero-capacity", (("A", "KEY"),), capacity="0"),
        _UncountedPageShape("numbered-pagination", (("A", "KEY"),), numbered_page=2),
        _UncountedPageShape(
            "unnumbered-pagination",
            (("A", "KEY"),),
            unnumbered_page=True,
        ),
        _UncountedPageShape(
            "repeated-page-action",
            (("A", "KEY"),),
            repeated_page_action=True,
        ),
        _UncountedPageShape("later-page", (("A", "KEY"),), current_page="2"),
        _UncountedPageShape("missing-page", (("A", "KEY"),), current_page=None),
    ],
    ids=lambda shape: shape.name,
)
def test_authority_rejects_ambiguous_uncounted_result_pages(
    case: _Case,
    shape: _UncountedPageShape,
) -> None:
    """Missing totals never imply completeness for ambiguous page shapes."""
    parse_error = _member(case, "ParseError")
    with pytest.raises(parse_error, match="reported result count"):
        getattr(case.module, "_parse_search_page")(
            _uncounted_result_page(
                shape.rows,
                capacity=shape.capacity,
                current_page=shape.current_page,
                numbered_page=shape.numbered_page,
                unnumbered_page=shape.unnumbered_page,
                repeated_page_action=shape.repeated_page_action,
            )
        )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_form_search_and_date_parser_boundaries(case: _Case) -> None:
    """Each authority rejects incomplete forms, routing rows, counts, and dates."""
    parse_error = _member(case, "ParseError")
    parse_form = getattr(case.module, "_parse_form")
    with pytest.raises(parse_error, match="form"):
        parse_form(b"<html></html>")
    with pytest.raises(parse_error, match="_csrf"):
        parse_form(b'<form><input name="week"></form>')

    soup = BeautifulSoup(
        '<form><input name="skip" type="submit"><select name="empty"></select>'
        '<textarea name="notes"> value </textarea><input name="odd"></form>',
        "html.parser",
    )
    form = soup.select_one("form")
    assert form is not None
    odd = form.select_one('input[name="odd"]')
    assert odd is not None
    odd["name"] = ["not-a-string"]  # type: ignore[assignment]
    form_fields = getattr(case.module, "_form_fields")
    fields = form_fields(form)
    assert [(field.name, field.value) for field in fields] == [
        ("empty", ""),
        ("notes", "value"),
    ]
    override_fields = getattr(case.module, "_override_fields")
    assert override_fields(form, {"extra": "value"})[-1].name == "extra"

    parse_search_page = getattr(case.module, "_parse_search_page")
    with pytest.raises(parse_error, match="summary link"):
        parse_search_page(b'<li class="searchresult"><p>Reference: A</p></li>')
    with pytest.raises(parse_error, match="keyVal"):
        parse_search_page(
            b'<li class="searchresult"><a href="applicationDetails.do">Details</a>'
            b"<p>Reference: A</p></li>"
        )
    with pytest.raises(parse_error, match="labelled reference"):
        parse_search_page(
            b'<div data-result-count="1"></div><li class="searchresult">'
            b'<a href="applicationDetails.do?keyVal=KEY">Details</a><p>Other</p></li>'
        )
    fallback = parse_search_page(
        b'<li class="searchresult"><a href="applicationDetails.do?keyVal=KEY">'
        b"Details</a><p>Reference: A</p></li> Displaying 1 of 1 results"
    )
    assert fallback.reported == 1
    reported_count = getattr(case.module, "_reported_count")
    assert (
        reported_count(
            BeautifulSoup("<p>No results found</p>", "html.parser"), row_count=0
        )
        == 0
    )
    assert (
        reported_count(
            BeautifulSoup("<p>Total 2 results</p>", "html.parser"), row_count=2
        )
        == 2
    )
    with pytest.raises(parse_error, match="reported result count"):
        reported_count(BeautifulSoup("<p>Unknown</p>", "html.parser"), row_count=0)

    labelled_value = getattr(case.module, "_labelled_value")
    prefix = BeautifulSoup("<p>Reference: A</p>", "html.parser").p
    assert prefix is not None
    assert labelled_value(prefix, "reference") == "A"
    with pytest.raises(parse_error, match="labelled missing"):
        labelled_value(prefix, "missing")

    parse_date = getattr(case.module, "_parse_date")
    assert parse_date(None) is None
    assert parse_date("2026-09-14") == date(2026, 9, 14)
    assert parse_date("14 September 2026") == date(2026, 9, 14)
    assert parse_date("14 Sep 2026") == date(2026, 9, 14)
    assert parse_date("bad") is None
    with pytest.raises(parse_error, match="fixture missing"):
        getattr(case.module, "_required_fixture")("value", "absent", "fixture missing")


@pytest.mark.parametrize("case", CASES[:3], ids=lambda case: str(case.authority_id))
def test_detail_parser_boundaries_are_authority_owned(case: _Case) -> None:
    """Each live detail parser models malformed, empty, and unavailable sections."""
    parse_error = _member(case, "ParseError")
    parse_summary = getattr(case.module, "_parse_summary")
    with pytest.raises(parse_error, match="simpleDetailsTable"):
        parse_summary(b"<html></html>")
    with pytest.raises(parse_error, match="summary labelled values"):
        parse_summary(
            b'<table id="simpleDetailsTable"><tr><td>orphan</td></tr></table>'
        )

    parse_documents = getattr(case.module, "_parse_documents")
    documents, state = parse_documents(b"<p>Information is not available</p>")
    assert documents == ()
    assert state.kind == "unavailable"
    assert getattr(case.module, "_is_unavailable")(
        BeautifulSoup("<p>Section is not available</p>", "html.parser")
    )
    documents, state = parse_documents(
        b'<div data-section="documents" data-count="0"></div>'
    )
    assert documents == ()
    assert state.kind == "empty"
    with pytest.raises(parse_error, match="documents table"):
        parse_documents(b'<div data-section="documents" data-count="1"></div>')
    with pytest.raises(parse_error, match="metadata link"):
        parse_documents(
            b'<div data-section="documents" data-count="1"></div>'
            b'<table summary="Documents"><tr><td>none</td></tr></table>'
        )
    documents, state = parse_documents(
        b'<div data-section="documents" data-count="1"></div>'
        b'<table summary="Documents"><tr><td><a href="file?id=1">Open</a>'
        b"</td></tr></table>"
    )
    assert documents[0].title == "Document"
    assert documents[0].published_date is None
    assert state.kind == "complete"
    documents, state = parse_documents(
        b'<div data-section="documents" data-count="0"></div>'
        b'<table summary="Documents"><tr><th>Heading</th></tr></table>'
    )
    assert documents == ()
    assert state.kind == "empty"
    with pytest.raises(parse_error, match="expected 2 actual 1"):
        parse_documents(
            b'<div data-section="documents" data-count="2"></div>'
            b'<table summary="Documents"><tr><td><a href="file?id=1">Open</a>'
            b"</td></tr></table>"
        )

    section_count = getattr(case.module, "_section_count")
    count_soup = BeautifulSoup(
        '<div data-section="other" data-count="9"></div>'
        '<div data-section="documents" data-count="2"></div>',
        "html.parser",
    )
    assert section_count(count_soup, "documents") == 2
    assert (
        section_count(BeautifulSoup("Documents (3)", "html.parser"), "documents") == 3
    )
    with pytest.raises(parse_error, match="displayed count"):
        section_count(BeautifulSoup("Unknown", "html.parser"), "documents")
    with pytest.raises(parse_error, match="summary proposal"):
        getattr(case.module, "_required_field")({}, "proposal")
    assert getattr(case.module, "_optional_date")({}, "received date") is None
    with pytest.raises(parse_error, match="date received date"):
        getattr(case.module, "_optional_date")(
            {"received date": "bad"},
            "received date",
        )


@pytest.mark.parametrize("case", CASES[:3], ids=lambda case: str(case.authority_id))
def test_live_detail_failure_states_and_reference_agreement(case: _Case) -> None:
    """Routing, summary identity, and child parsing failures remain explicit."""
    adapter = _member(case, "Adapter")()
    reference = SourceReference(
        source_id=case.source_id,
        reference=case.references[0],
        locator=case.locators[0],
    )

    async def fetch(mock: _IdoxMock, source: SourceReference = reference) -> Any:
        session = _session(mock)
        try:
            return await adapter.fetch(session, source)
        finally:
            await session.aclose()

    with pytest.raises(ValueError, match="keyVal locator"):
        asyncio.run(
            fetch(
                _IdoxMock(case),
                reference.model_copy(update={"locator": None}),
            )
        )
    with pytest.raises(ValueError, match="reference mismatch"):
        asyncio.run(fetch(_IdoxMock(case, summary_mismatch=True)))
    malformed = asyncio.run(fetch(_IdoxMock(case, documents_malformed=True)))
    assert malformed.completeness.documents.kind == "failed"

    if case.authority_id != AuthorityId("west-suffolk"):
        unavailable = asyncio.run(fetch(_IdoxMock(case, comments_fail=True)))
        assert unavailable.completeness.comments.kind == "failed"
        malformed_comments = asyncio.run(
            fetch(_IdoxMock(case, comments_malformed=True))
        )
        assert malformed_comments.completeness.comments.kind == "failed"
        nonzero = asyncio.run(fetch(_IdoxMock(case, comments_nonzero=True)))
        assert nonzero.completeness.comments.kind == "unavailable"


def test_west_suffolk_qualification_requires_explicit_safe_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject implicit live access, incomplete scope, and unsafe target reuse."""
    module = _qualification_module()
    created = 0

    def session_factory() -> _QualificationSession:
        nonlocal created
        created += 1
        return _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))

    base = [
        "--data-dir",
        str(tmp_path / "missing-confirmation"),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
        "--include-open",
    ]
    assert module.main(base, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "confirmation-required"

    without_open = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "missing-open"),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
    ]
    assert module.main(without_open, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "include-open-required"

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("preserve", encoding="utf-8")
    args = [
        "--confirm-live",
        "--data-dir",
        str(occupied),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
        "--include-open",
    ]
    assert module.main(args, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert (occupied / "existing").read_text(encoding="utf-8") == "preserve"
    assert created == 0


def test_west_suffolk_qualification_persists_and_proves_idempotence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Qualify through durable collection and a zero-I/O terminal rerun."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))
        sessions.append(session)
        return session

    args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
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
    assert len(sessions[0].requested_urls) == 39
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "west-suffolk"
    assert receipt["created_at"] == "2026-09-16T12:00:00Z"
    assert receipt["scope"] == {
        "start": "2026-09-14",
        "end": "2026-09-20",
        "include_open": True,
    }
    assert receipt["counts"] == {
        "applications": 12,
        "discovered_references": 12,
        "native_versions": 12,
        "application_versions": 12,
        "document_versions": 12,
        "comment_versions": 0,
        "pending_retries": 0,
        "failed_sections": 0,
        "unmapped_records": 0,
    }
    assert receipt["costs"]["initial"]["request_count"] == 39
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert {check["name"]: check["ok"] for check in receipt["checks"]} == {
        "application-count": True,
        "attachment-policy": True,
        "database-integrity": True,
        "evidence-paths": True,
        "failed-sections": True,
        "idempotent-rerun": True,
        "pending-retries": True,
        "run-statuses": True,
        "terminal-checkpoint": True,
        "terminal-rerun-requests": True,
        "unmapped-records": True,
    }
    receipt_path = data_dir / "west-suffolk-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert not (data_dir / ".west-suffolk-qualification-v1.json.tmp").exists()

    sessions.clear()
    assert (
        module.main(
            [*args, "--resume"],
            session_factory=session_factory,
            now=lambda: datetime(2026, 9, 16, 13, tzinfo=UTC),
        )
        == 0
    )
    resumed_receipt = json.loads(capsys.readouterr().out)
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert all(session.requested_urls == () for session in sessions)
    assert resumed_receipt["costs"]["initial"]["request_count"] == 0
    assert resumed_receipt["costs"]["rerun"]["request_count"] == 0


def test_west_suffolk_qualification_restarts_a_wrong_scope_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminal checkpoint qualifies only the exact scope that produced it."""
    module = _qualification_module()
    data_dir = tmp_path / "wrong-scope"
    store = _store(data_dir)
    run_id = store.begin_run(AuthorityId("west-suffolk"))
    checkpoint = west_suffolk_adapter.WestSuffolkCheckpointV1(
        result_page="live",
        live_scope=west_suffolk_adapter.WestSuffolkDiscoveryScope(
            start=date(2026, 9, 7),
            end=date(2026, 9, 13),
            include_open=True,
        ),
        live_complete=True,
    )
    store.commit_discovery(
        run_id,
        AuthorityId("west-suffolk"),
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=StoredCheckpoint(
                schema_version=1,
                payload_json=checkpoint.model_dump_json(),
            ),
            complete=True,
        ),
    )
    store.finish_run(
        run_id,
        AuthorityId("west-suffolk"),
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=0,
                transferred_bytes=0,
                duration_ms=0,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.LIVE,
        ),
    )
    store.close()
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))
        sessions.append(session)
        return session

    result = module.main(
        [
            "--confirm-live",
            "--resume",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-09-14",
            "--end",
            "2026-09-20",
            "--include-open",
        ],
        session_factory=session_factory,
    )

    assert result == 0
    assert len(sessions[0].requested_urls) == 39
    assert sessions[1].requested_urls == ()
    assert json.loads(capsys.readouterr().out)["scope"] == {
        "start": "2026-09-14",
        "end": "2026-09-20",
        "include_open": True,
    }


def test_west_suffolk_qualification_rejects_failed_current_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a receipt while any current section remains failed."""
    module = _qualification_module()
    data_dir = tmp_path / "failed-sections"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(
            _IdoxMock(_WEST_SUFFOLK_CASE, documents_fail=True)
        )
        sessions.append(session)
        return session

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-09-14",
            "--end",
            "2026-09-20",
            "--include-open",
        ],
        session_factory=session_factory,
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert not (data_dir / "west-suffolk-qualification-v1.json").exists()


def test_west_suffolk_qualification_rejects_incoherent_terminal_checkpoints(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminal bit alone cannot certify a discovery scope."""
    module = _qualification_module()
    data_dir = tmp_path / "checkpoint-coherence"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))
        sessions.append(session)
        return session

    args = [
        "--confirm-live",
        "--resume",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
        "--include-open",
    ]
    assert module.main(args, session_factory=session_factory) == 0
    capsys.readouterr()
    store = _store(data_dir)
    stored = store.discovery_state(AuthorityId("west-suffolk")).checkpoint
    assert stored is not None
    baseline = west_suffolk_adapter.WestSuffolkCheckpointV1.model_validate_json(
        stored.payload_json
    )
    store.close()
    assert len(baseline.completed_queries) == 10
    assert len(baseline.seen_references) == 12

    corruptions = (
        baseline.model_copy(update={"completed_queries": ()}),
        baseline.model_copy(
            update={
                "completed_queries": (
                    *baseline.completed_queries,
                    baseline.completed_queries[0],
                )
            }
        ),
        baseline.model_copy(
            update={
                "active_query": baseline.completed_queries[0],
                "next_page": 2,
                "query_row_count": 1,
            }
        ),
        baseline.model_copy(update={"seen_references": baseline.seen_references[:-1]}),
    )
    receipt_path = data_dir / "west-suffolk-qualification-v1.json"
    for checkpoint in corruptions:
        store = _store(data_dir)
        run_id = store.begin_run(AuthorityId("west-suffolk"))
        store.commit_discovery(
            run_id,
            AuthorityId("west-suffolk"),
            DurableDiscoveryBatch(
                references=(),
                next_checkpoint=StoredCheckpoint(
                    schema_version=1,
                    payload_json=checkpoint.model_dump_json(),
                ),
                complete=True,
            ),
        )
        store.finish_run(
            run_id,
            AuthorityId("west-suffolk"),
            RunOutcome(
                status=RunStatus.SUCCEEDED,
                metrics=RunMetrics(
                    request_count=0,
                    transferred_bytes=0,
                    duration_ms=0,
                    storage_growth_bytes=0,
                ),
                transport_mode=TransportMode.LIVE,
            ),
        )
        store.close()
        receipt_path.unlink(missing_ok=True)
        sessions.clear()

        assert module.main(args, session_factory=session_factory) == 1
        error = json.loads(capsys.readouterr().err)
        assert "terminal-checkpoint" in error["failed_checks"]
        assert len(sessions) == 1
        assert sessions[0].requested_urls == ()
        assert sessions[0].closed is True
        assert not receipt_path.exists()

    empty_dir = tmp_path / "empty-terminal"
    empty_store = _store(empty_dir)
    run_id = empty_store.begin_run(AuthorityId("west-suffolk"))
    empty = baseline.model_copy(update={"seen_references": ()})
    empty_store.commit_discovery(
        run_id,
        AuthorityId("west-suffolk"),
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=StoredCheckpoint(
                schema_version=1,
                payload_json=empty.model_dump_json(),
            ),
            complete=True,
        ),
    )
    empty_store.finish_run(
        run_id,
        AuthorityId("west-suffolk"),
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=0,
                transferred_bytes=0,
                duration_ms=0,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.LIVE,
        ),
    )
    empty_store.close()
    empty_sessions: list[_QualificationSession] = []

    def empty_session_factory() -> _QualificationSession:
        session = _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))
        empty_sessions.append(session)
        return session

    empty_args = args.copy()
    empty_args[3] = str(empty_dir)
    assert module.main(empty_args, session_factory=empty_session_factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert {"application-count", "terminal-checkpoint"}.issubset(error["failed_checks"])
    assert len(empty_sessions) == 1
    assert empty_sessions[0].requested_urls == ()
    assert empty_sessions[0].closed is True


def test_west_suffolk_qualification_accepts_live_week_key_rendering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Treat the recorded textual Monday rendering as the same week."""
    module = _qualification_module()
    data_dir = tmp_path / "live-week-rendering"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_IdoxMock(_WEST_SUFFOLK_CASE))
        sessions.append(session)
        return session

    args = [
        "--confirm-live",
        "--resume",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-09-14",
        "--end",
        "2026-09-20",
        "--include-open",
    ]
    assert module.main(args, session_factory=session_factory) == 0
    capsys.readouterr()
    store = _store(data_dir)
    stored = store.discovery_state(AuthorityId("west-suffolk")).checkpoint
    assert stored is not None
    checkpoint = west_suffolk_adapter.WestSuffolkCheckpointV1.model_validate_json(
        stored.payload_json
    )
    live_keys = tuple(
        key.replace("14/09/2026", "14 Sep 2026") for key in checkpoint.completed_queries
    )
    run_id = store.begin_run(AuthorityId("west-suffolk"))
    store.commit_discovery(
        run_id,
        AuthorityId("west-suffolk"),
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=StoredCheckpoint(
                schema_version=1,
                payload_json=checkpoint.model_copy(
                    update={"completed_queries": live_keys}
                ).model_dump_json(),
            ),
            complete=True,
        ),
    )
    store.finish_run(
        run_id,
        AuthorityId("west-suffolk"),
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=0,
                transferred_bytes=0,
                duration_ms=0,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.LIVE,
        ),
    )
    store.close()
    (data_dir / "west-suffolk-qualification-v1.json").unlink()
    sessions.clear()

    assert module.main(args, session_factory=session_factory) == 0
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)
    assert all(session.closed for session in sessions)
