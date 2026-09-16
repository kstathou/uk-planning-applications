# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: C901, D103, E501, EM102, PLR0911, PLR0912, PLR0913, PLR0915, PLR2004, SLF001, TRY003

"""Barnet authority-native IDOX live-boundary behaviour."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import date
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import HttpUrl, ValidationError

import yimby.authorities.barnet.adapter as barnet_adapter
from yimby import AuthorityId, Collector, DiscoveryWindow, barnet_registry
from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.barnet.adapter import (
    BarnetAdapter,
    BarnetCheckpointError,
    BarnetCheckpointV1,
    BarnetCountMismatchError,
    BarnetParseError,
    BarnetReferenceMismatchError,
    BarnetRoutingError,
)
from yimby.domain import (
    DiscoveryBatch,
    EmptySection,
    SourceId,
    SourceReference,
    UnavailableSection,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.store import SqliteStore
from yimby.transport import FormField, PortalRequest, RequestIntent, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

WEEK = DiscoveryWindow(
    start=date(2026, 9, 14),
    end=date(2026, 9, 20),
    include_open=False,
)
WEEK_WITH_OPEN = WEEK.model_copy(update={"include_open": True})

WEEKLY_FORM = b"""
<!doctype html><html><body>
<form action="weeklyListResults.do?action=firstPage" method="post">
  <input type="hidden" name="_csrf" value="sanitised-csrf">
  <select name="searchCriteria.ward"><option value="" selected>All</option></select>
  <select name="week">
    <option value="07/09/2026">7 September</option>
    <option value="14/09/2026">14 September</option>
    <option value="21/09/2026">21 September</option>
  </select>
  <input type="radio" name="dateType" value="DC_Validated" checked>
  <input type="radio" name="dateType" value="DC_Decided">
  <input type="checkbox" name="unused" value="must-not-be-posted">
  <input type="hidden" name="searchType" value="Application">
  <input type="submit" name="submit" value="Search">
</form></body></html>
"""

OPEN_CASE_STATUSES = (
    "Application Received",
    "Valid Application Received",
    "Pending Consideration",
    "Pending Decision",
)
ACTIVE_APPEAL_STATUSES = (
    "Appeal in progress",
    "Appeal lodged",
    "Appeal Valid",
    "High Court Appeal Lodged",
    "Remitted to Secretary of State",
)
ADVANCED_FORM = f"""
<!doctype html><html><body>
<form id="advancedSearchForm"
      action="advancedSearchResults.do?action=firstPage" method="post">
  <input type="hidden" name="_csrf" value="">
  <select name="searchCriteria.caseStatus">
    <option value="" selected>All</option>
    {"".join(f'<option value="{value}">{value}</option>' for value in OPEN_CASE_STATUSES)}
  </select>
  <select name="searchCriteria.appealStatus">
    <option value="" selected>All</option>
    {"".join(f'<option value="{value}">{value}</option>' for value in ACTIVE_APPEAL_STATUSES)}
  </select>
  <input type="hidden" name="caseAddressType" value="">
  <input type="hidden" name="searchType" value="">
  <input name="date(applicationReceivedStart)" value="">
  <input name="date(applicationReceivedEnd)" value="">
  <input type="hidden" name="tag" value="one">
  <input type="hidden" name="tag" value="two">
</form></body></html>
""".encode()

SUMMARY = b"""
<!doctype html><html><body><table id="simpleDetailsTable">
<tr><th>Reference</th><td>TCP/0001/26</td></tr>
<tr><th>Alternative Reference</th><td>PP-000001</td></tr>
<tr><th>Application Type</th><td>Full Planning Permission</td></tr>
<tr><th>Address</th><td>1 Example Road, Barnet</td></tr>
<tr><th>Proposal</th><td>Plant replacement trees</td></tr>
<tr><th>Status</th><td>Registered</td></tr>
<tr><th>Received Date</th><td>14/09/2026</td></tr>
<tr><th>Validated Date</th><td>15/09/2026</td></tr>
<tr><th>Decision Date</th><td>20/09/2026</td></tr>
<tr><th>Decision</th><td>Granted</td></tr>
<tr><th>Case Officer</th><td>Officer removed</td></tr>
</table></body></html>
"""

DOCUMENTS = b"""
<!doctype html><html><body>
<h2 data-section="documents" data-count="2">Documents (2)</h2>
<table summary="Documents"><thead><tr>
<th>Date Published</th><th>Document Type</th><th>Drawing Number</th>
<th>Description</th><th>View</th></tr></thead><tbody>
<tr><td>16/09/2026</td><td>Plan</td><td>A-01</td><td>Site plan</td>
<td><a href="files/site-plan.pdf">View</a></td></tr>
<tr><td>17/09/2026</td><td>Report</td><td></td><td>Tree report</td>
<td><a href="files/tree-report.pdf">View</a></td></tr>
</tbody></table></body></html>
"""

PUBLIC_COMMENTS = b"""
<!doctype html><html><body>
<h2 data-section="public comments" data-count="1">Public Comments (1)</h2>
<table summary="Public Comments"><thead><tr><th>Comment</th></tr></thead><tbody>
<tr data-comment-id="sanitised-1"><td>Support recorded.</td></tr>
</tbody></table></body></html>
"""

CONSULTEE_COMMENTS = b"""
<!doctype html><html><body>
<h2 data-section="consultee comments" data-count="0">Consultee Comments (0)</h2>
</body></html>
"""


def _result_page(
    references: tuple[tuple[str, str], ...],
    *,
    count: int,
    pages: int = 1,
    start: int = 1,
    current_page: int = 1,
) -> bytes:
    rows = "".join(
        (
            '<li class="searchresult">'
            f'<a href="applicationDetails.do?keyVal={locator}&amp;activeTab=summary">'
            "Details</a>"
            f"<p><span>Reference:</span><span>{reference}</span></p></li>"
        )
        for reference, locator in references
    )
    pagination = (
        ""
        if pages == 1
        else "".join(
            (
                '<a href="pagedSearchResults.do?action=page&amp;'
                f'searchCriteria.page={page}">{page}</a>'
            )
            for page in range(1, pages + 1)
        )
    )
    displayed_range = (
        ""
        if pages == 1 or not references
        else (
            '<p class="pager"><span class="showing">'
            f"Showing {start}-{start + len(references) - 1} of {count}"
            "</span></p>"
            f'<input name="searchCriteria.page" value="{current_page}">'
        )
    )
    return (
        "<!doctype html><html><body>"
        f'<div data-result-count="{count}"></div>{displayed_range}'
        f"<ul>{rows}</ul>{pagination}"
        "</body></html>"
    ).encode()


def _uncounted_result_page(
    references: tuple[tuple[str, str], ...],
    *,
    capacity: str | None = "10",
    current_page: str | None = "1",
    numbered_page: int | None = None,
    repeated_page_action: bool = False,
) -> bytes:
    rows = "".join(
        (
            '<li class="searchresult">'
            f'<a href="applicationDetails.do?keyVal={locator}">Details</a>'
            f"<p>Ref. No: {reference}</p></li>"
        )
        for reference, locator in references
    )
    page_control = (
        ""
        if current_page is None
        else f'<input name="searchCriteria.page" value="{current_page}">'
    )
    capacity_control = (
        ""
        if capacity is None
        else (
            '<select name="searchCriteria.resultsPerPage">'
            f'<option value="{capacity}" selected>{capacity}</option></select>'
        )
    )
    pagination = ""
    if numbered_page is not None:
        pagination = (
            '<a href="pagedSearchResults.do?action=page&amp;searchCriteria.page='
            f'{numbered_page}">next</a>'
        )
    if repeated_page_action:
        pagination = (
            '<a href="pagedSearchResults.do?action=page&amp;action=printPreview">'
            "next</a>"
        )
    return (
        f"<form>{page_control}{capacity_control}</form><ul>{rows}</ul>{pagination}"
    ).encode()


def _showing_result_page(
    references: tuple[tuple[str, str], ...],
    markers: tuple[str, ...],
    *,
    current_page: str | None = "1",
    visible_pages: tuple[str, ...] = (),
    capacity: str | None = "10",
    numbered_page: int | None = None,
) -> bytes:
    pagers = "".join(
        '<p class="pager"><span class="showing">'
        f"{marker}</span>"
        f"{''.join(f'<strong>{page}</strong>' for page in visible_pages)}"
        "</p>"
        for marker in markers
    )
    return pagers.encode() + _uncounted_result_page(
        references,
        capacity=capacity,
        current_page=current_page,
        numbered_page=numbered_page,
    )


class _BarnetMock:
    def __init__(
        self,
        *,
        multi_page: bool = False,
        count_mismatch: bool = False,
        open_message: bytes = b"Too many results found. Please enter some more parameters.",
        child_failure: bool = False,
        child_unavailable: bool = False,
        summary: bytes = SUMMARY,
        advanced_multi_page: bool = False,
    ) -> None:
        self.multi_page = multi_page
        self.count_mismatch = count_mismatch
        self.open_message = open_message
        self.child_failure = child_failure
        self.child_unavailable = child_unavailable
        self.summary = summary
        self.advanced_multi_page = advanced_multi_page
        self.active_advanced: tuple[str, str] | None = None
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
                content=WEEKLY_FORM,
            )
        if path.endswith("/weeklyListResults.do"):
            assert request.headers.get("cookie") == "JSESSIONID=sanitised"
            submitted = dict(fields)
            assert submitted["_csrf"] == "sanitised-csrf"
            assert submitted["searchCriteria.ward"] == ""
            assert submitted["week"] == "14/09/2026"
            assert submitted["searchType"] == "Application"
            assert [value for name, value in fields if name == "dateType"] == [
                submitted["dateType"]
            ]
            assert "unused" not in submitted
            if submitted["dateType"] == "DC_Decided":
                if self.multi_page:
                    return httpx.Response(
                        200,
                        content=_result_page(
                            (("TCP/0002/26", "KEY-2"), ("TCP/0004/26", "KEY-4")),
                            count=2,
                        ),
                    )
                return httpx.Response(
                    200,
                    content=_result_page((("TCP/0001/26", "KEY-1"),), count=1),
                )
            if self.multi_page:
                return httpx.Response(
                    200,
                    content=_result_page(
                        (("TCP/0001/26", "KEY-1"), ("TCP/0002/26", "KEY-2")),
                        count=99 if self.count_mismatch else 3,
                        pages=2,
                    ),
                )
            return httpx.Response(
                200,
                content=_result_page((("TCP/0001/26", "KEY-1"),), count=1),
            )
        if path.endswith("/pagedSearchResults.do") and self.active_advanced is not None:
            _field, value = self.active_advanced
            index = (*OPEN_CASE_STATUSES, *ACTIVE_APPEAL_STATUSES).index(value) + 1
            return httpx.Response(
                200,
                content=_result_page(
                    ((f"ADV/{index:04d}/26-B", f"ADV-{index}-B"),),
                    count=2,
                    pages=2,
                    start=2,
                    current_page=2,
                ),
            )
        if path.endswith("/pagedSearchResults.do"):
            page = int(request.url.params.get("searchCriteria.page", "1"))
            rows = (
                () if self.count_mismatch and page > 2 else (("TCP/0003/26", "KEY-3"),)
            )
            return httpx.Response(
                200,
                content=_result_page(
                    rows,
                    count=99 if self.count_mismatch else 3,
                    pages=2 if rows else 1,
                    start=3,
                    current_page=page,
                ),
            )
        if path.endswith("/search.do") and action == "advanced":
            return httpx.Response(200, content=ADVANCED_FORM)
        if path.endswith("/advancedSearchResults.do"):
            submitted = dict(fields)
            selected = tuple(
                (field, submitted[field])
                for field in (
                    "searchCriteria.caseStatus",
                    "searchCriteria.appealStatus",
                )
                if submitted[field]
            )
            if submitted["date(applicationReceivedStart)"]:
                assert selected == ()
                assert submitted["date(applicationReceivedStart)"] == "14/09/2026"
                assert submitted["date(applicationReceivedEnd)"] == "20/09/2026"
                reference = "TCP/0001/26"
                locator = "KEY-1"
                index = 0
            else:
                assert len(selected) == 1
                self.active_advanced = selected[0]
                _field, value = selected[0]
                index = (*OPEN_CASE_STATUSES, *ACTIVE_APPEAL_STATUSES).index(value) + 1
                reference = "TCP/0001/26" if index == 1 else f"ADV/{index:04d}/26"
                locator = "KEY-1" if index == 1 else f"ADV-{index}"
            return httpx.Response(
                200,
                content=_result_page(
                    ((reference, locator),),
                    count=(2 if self.advanced_multi_page and index == 1 else 1),
                    pages=(2 if self.advanced_multi_page and index == 1 else 1),
                ),
            )
        if path.endswith("/applicationDetails.do"):
            active_tab = request.url.params["activeTab"]
            assert request.url.params["keyVal"] == "KEY-1"
            if active_tab == "summary":
                return httpx.Response(200, content=self.summary)
            if self.child_unavailable:
                return httpx.Response(404)
            if active_tab == "documents":
                content = (
                    b'<div data-section="documents" data-count="1"></div>'
                    if self.child_failure
                    else DOCUMENTS
                )
                return httpx.Response(200, content=content)
            if active_tab == "neighbourComments":
                content = (
                    b'<div data-section="public comments" data-count="1"></div>'
                    if self.child_failure
                    else PUBLIC_COMMENTS
                )
                return httpx.Response(200, content=content)
            if active_tab == "consulteeComments":
                return httpx.Response(200, content=CONSULTEE_COMMENTS)
        if path.endswith((".pdf", ".doc", ".docx")):
            self.attachment_paths.append(path)
            return httpx.Response(500)
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def _session(mock: _BarnetMock) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
    )


def _store(root: Path) -> SqliteStore:
    return SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))


def test_live_discovery_pages_deduplicates_and_resumes() -> None:
    """Weekly validated and decided pages retain keyVal and resume by reposting."""
    adapter = BarnetAdapter()
    first_mock = _BarnetMock(multi_page=True)
    first_session = _session(first_mock)

    async def first_page() -> DiscoveryBatch[BarnetCheckpointV1]:
        batches = cast(
            "AsyncGenerator[DiscoveryBatch[BarnetCheckpointV1]]",
            adapter.discover(first_session, WEEK, None),
        )
        batch = await anext(batches)
        await batches.aclose()
        await first_session.aclose()
        return batch

    first = asyncio.run(first_page())
    assert [reference.reference for reference in first.references] == [
        "TCP/0001/26",
        "TCP/0002/26",
    ]
    assert [reference.locator for reference in first.references] == ["KEY-1", "KEY-2"]
    assert first.next_checkpoint.active_query == "weekly|2026-09-14|DC_Validated"
    assert first.next_checkpoint.next_page == 2

    resumed_mock = _BarnetMock(multi_page=True)
    resumed_session = _session(resumed_mock)

    async def resume() -> list[DiscoveryBatch[BarnetCheckpointV1]]:
        batches = [
            batch
            async for batch in adapter.discover(
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
    ] == ["TCP/0003/26", "TCP/0004/26"]
    assert resumed[-1].complete
    assert resumed[-1].next_checkpoint.live_complete
    posts = [
        request
        for request in resumed_mock.requests
        if request[0] == "POST" and request[1].endswith("/weeklyListResults.do")
    ]
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


def test_live_discovery_rejects_count_mismatch() -> None:
    mismatch_session = _session(_BarnetMock(multi_page=True, count_mismatch=True))

    async def mismatch() -> None:
        with pytest.raises(BarnetCountMismatchError, match="expected 99 actual 3"):
            async for _batch in BarnetAdapter().discover(
                mismatch_session,
                WEEK,
                None,
            ):
                pass
        await mismatch_session.aclose()

    asyncio.run(mismatch())


def test_live_discovery_exhausts_exact_open_and_appeal_inventory() -> None:
    mock = _BarnetMock()
    session = _session(mock)

    async def discover() -> list[DiscoveryBatch[BarnetCheckpointV1]]:
        batches = [
            batch
            async for batch in BarnetAdapter().discover(
                session,
                WEEK_WITH_OPEN,
                None,
            )
        ]
        await session.aclose()
        return batches

    batches = asyncio.run(discover())
    checkpoint = batches[-1].next_checkpoint
    scope = barnet_adapter.BarnetDiscoveryScope(
        start=WEEK.start,
        end=WEEK.end,
        include_open=True,
    )
    expected = barnet_adapter.expected_live_query_keys(scope)
    assert checkpoint.completed_queries == expected
    assert checkpoint.live_scope == scope
    assert checkpoint.live_complete
    assert len(expected) == 12
    assert expected[2] == "advanced|received|2026-09-14|2026-09-20"
    assert expected[-9:] == tuple(
        f"advanced|{field}|{value}"
        for field, values in (
            ("searchCriteria.caseStatus", OPEN_CASE_STATUSES),
            ("searchCriteria.appealStatus", ACTIVE_APPEAL_STATUSES),
        )
        for value in values
    )
    assert [
        reference.reference for batch in batches for reference in batch.references
    ] == ["TCP/0001/26", *(f"ADV/{index:04d}/26" for index in range(2, 10))]
    advanced_posts = [
        fields
        for method, path, fields in mock.requests
        if method == "POST" and path.endswith("/advancedSearchResults.do")
    ]
    assert len(advanced_posts) == 10
    received_post = dict(advanced_posts[0])
    assert received_post["date(applicationReceivedStart)"] == "14/09/2026"
    assert received_post["date(applicationReceivedEnd)"] == "20/09/2026"
    assert [
        value for fields in advanced_posts for name, value in fields if name == "tag"
    ] == ["one", "two"] * 10


def test_live_advanced_discovery_resumes_by_reposting_first_page() -> None:
    adapter = BarnetAdapter()
    first_session = _session(_BarnetMock(advanced_multi_page=True))

    async def stop_on_advanced_page() -> DiscoveryBatch[BarnetCheckpointV1]:
        stream = cast(
            "AsyncGenerator[DiscoveryBatch[BarnetCheckpointV1]]",
            adapter.discover(first_session, WEEK_WITH_OPEN, None),
        )
        await anext(stream)
        await anext(stream)
        await anext(stream)
        batch = await anext(stream)
        await stream.aclose()
        await first_session.aclose()
        return batch

    first = asyncio.run(stop_on_advanced_page())
    assert first.next_checkpoint.active_query == (
        "advanced|searchCriteria.caseStatus|Application Received"
    )
    assert first.next_checkpoint.next_page == 2

    resumed_mock = _BarnetMock(advanced_multi_page=True)
    resumed_session = _session(resumed_mock)

    async def resume() -> list[DiscoveryBatch[BarnetCheckpointV1]]:
        batches = [
            batch
            async for batch in adapter.discover(
                resumed_session,
                WEEK_WITH_OPEN,
                first.next_checkpoint,
            )
        ]
        await resumed_session.aclose()
        return batches

    resumed = asyncio.run(resume())
    assert resumed[0].references[0].reference == "ADV/0001/26-B"
    requests = [
        path
        for method, path, _fields in resumed_mock.requests
        if method in {"GET", "POST"}
    ]
    advanced_post = requests.index("/online-applications/advancedSearchResults.do")
    advanced_page = requests.index("/online-applications/pagedSearchResults.do")
    assert advanced_post < advanced_page
    assert resumed[-1].complete

    uninterrupted_session = _session(_BarnetMock(advanced_multi_page=True))

    async def uninterrupted() -> list[DiscoveryBatch[BarnetCheckpointV1]]:
        batches = [
            batch
            async for batch in adapter.discover(
                uninterrupted_session,
                WEEK_WITH_OPEN,
                None,
            )
        ]
        await uninterrupted_session.aclose()
        return batches

    assert asyncio.run(uninterrupted())[-1].complete


def test_live_collection_persists_locator_metadata_and_child_failures(
    tmp_path: Path,
) -> None:
    """Public collection stores keyVal and never follows document links."""
    store = _store(tmp_path)
    collector = Collector(barnet_registry(), store)
    mock = _BarnetMock()
    session = _session(mock)
    report = asyncio.run(collector.collect(AuthorityId("barnet"), WEEK, session))
    asyncio.run(session.aclose())
    assert len(report.applications) == 1
    application_id = report.applications[0]
    application = store.get_application(application_id)
    assert application.reference == "TCP/0001/26"
    assert [document.title for document in application.documents] == [
        "Site plan",
        "Tree report",
    ]
    assert [comment.text for comment in application.comments] == ["Support recorded."]
    view = store.application_view(application_id)
    assert str(view.metadata.source_url) == (
        "https://publicaccess.barnet.gov.uk/online-applications/applicationDetails.do"
    )
    assert view.metadata.aliases == ("PP-000001",)
    assert view.metadata.application_type == "Full Planning Permission"
    assert view.metadata.decision == "Granted"
    assert view.metadata.address == "1 Example Road, Barnet"
    assert view.metadata.received_date == date(2026, 9, 14)
    assert view.metadata.validated_date == date(2026, 9, 15)
    assert view.metadata.decision_date == date(2026, 9, 20)
    queued = store.discovery_state(AuthorityId("barnet")).queued
    assert queued == (
        SourceReference(
            source_id=SourceId("barnet-idox-current"),
            reference="TCP/0001/26",
            locator="KEY-1",
        ),
    )
    retained = store.retained_native_records()[0]
    assert retained.reference.locator == "KEY-1"
    assert BARNET_PACKAGE.rebuild(retained).reference.locator == "KEY-1"
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute(
            "SELECT locator FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("KEY-1",)
    assert mock.attachment_paths == []
    assert report.attachment_body_requests == 0

    failing_mock = _BarnetMock(child_failure=True)
    failing_session = _session(failing_mock)

    async def commit_failed_children() -> None:
        collected = await BARNET_PACKAGE.collect(failing_session, queued[0])
        run_id = store.begin_run(AuthorityId("barnet"))
        store.commit_observation(run_id, collected)
        await failing_session.aclose()

    asyncio.run(commit_failed_children())
    preserved = store.get_application(application_id)
    assert len(preserved.documents) == 2
    assert len(preserved.comments) == 1
    assert preserved.completeness.documents.kind == "failed"
    assert preserved.completeness.comments.kind == "failed"
    assert store.semantic_version_count(application_id, "documents") == 1
    assert store.semantic_version_count(application_id, "comments") == 1
    assert failing_mock.attachment_paths == []
    store.close()


def test_live_fetch_requires_locator_and_form_transport_preserves_pairs() -> None:
    """Human references are not routing keys and ordered repeated form fields survive."""
    missing_session = _session(_BarnetMock())

    async def missing_locator() -> None:
        with pytest.raises(BarnetRoutingError, match="TCP/0001/26"):
            await BarnetAdapter().fetch(
                missing_session,
                SourceReference(
                    source_id=SourceId("barnet-idox-current"),
                    reference="TCP/0001/26",
                ),
            )
        await missing_session.aclose()

    asyncio.run(missing_locator())

    bodies: list[bytes] = []

    def form_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, content=b"ok")

    form_session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(form_handler)),
        limiter=HostRateLimiter(0),
    )

    async def post_form() -> None:
        await form_session.fetch(
            PortalRequest(
                url=HttpUrl("https://example.test/form?secret=redacted"),
                intent=RequestIntent.SEARCH,
                method=RequestMethod.POST,
                form=(
                    FormField(name="tag", value="one"),
                    FormField(name="tag", value="two"),
                    FormField(name="blank", value=""),
                ),
            )
        )
        await form_session.aclose()

    asyncio.run(post_form())
    assert bodies == [b"tag=one&tag=two&blank="]
    assert form_session.requested_urls == ("https://example.test/form",)
    with pytest.raises(ValidationError, match="cannot carry form"):
        PortalRequest(
            url=HttpUrl("https://example.test/form"),
            intent=RequestIntent.SEARCH,
            form=(FormField(name="bad", value="get"),),
        )


def test_live_discovery_checkpoint_edges_are_explicit() -> None:
    adapter = BarnetAdapter()

    async def exercise() -> None:
        scope = barnet_adapter.BarnetDiscoveryScope(
            start=WEEK.start,
            end=WEEK.end,
            include_open=False,
        )
        terminal_session = _session(_BarnetMock())
        terminal = [
            batch
            async for batch in adapter.discover(
                terminal_session,
                WEEK,
                BarnetCheckpointV1(
                    cursor="live",
                    live_scope=scope,
                    completed_queries=barnet_adapter.expected_live_query_keys(scope),
                    seen_references=("TCP/0001/26",),
                    live_complete=True,
                ),
            )
        ]
        assert len(terminal) == 1
        assert terminal[0].complete
        assert terminal_session.requested_urls == ()
        await terminal_session.aclose()

        corrupt_terminal_session = _session(_BarnetMock())
        with pytest.raises(BarnetCheckpointError, match="terminal"):
            async for _batch in adapter.discover(
                corrupt_terminal_session,
                WEEK,
                BarnetCheckpointV1(
                    cursor="live",
                    live_scope=scope,
                    live_complete=True,
                ),
            ):
                pass
        assert corrupt_terminal_session.requested_urls == ()
        await corrupt_terminal_session.aclose()

        stale_session = _session(_BarnetMock())
        with pytest.raises(BarnetCheckpointError, match="unavailable"):
            async for _batch in adapter.discover(
                stale_session,
                WEEK,
                BarnetCheckpointV1(
                    cursor="live",
                    live_scope=scope,
                    active_query="weekly|2000-01-03|DC_Validated",
                ),
            ):
                pass
        await stale_session.aclose()

        wrong_scope = barnet_adapter.BarnetDiscoveryScope(
            start=date(2026, 9, 7),
            end=date(2026, 9, 13),
            include_open=False,
        )
        restarted_session = _session(_BarnetMock())
        restarted = [
            batch
            async for batch in adapter.discover(
                restarted_session,
                WEEK,
                BarnetCheckpointV1(
                    cursor="live",
                    live_scope=wrong_scope,
                    completed_queries=barnet_adapter.expected_live_query_keys(
                        wrong_scope
                    ),
                    live_complete=True,
                ),
            )
        ]
        assert restarted[-1].complete
        assert restarted[-1].next_checkpoint.live_scope == scope
        assert restarted_session.requested_urls
        await restarted_session.aclose()

        for include_open in (False, True):
            recovery_window = WEEK.model_copy(update={"include_open": include_open})
            recovery_scope = barnet_adapter.BarnetDiscoveryScope(
                start=recovery_window.start,
                end=recovery_window.end,
                include_open=include_open,
            )
            recovery_session = _session(_BarnetMock())
            recovered = [
                batch
                async for batch in adapter.discover(
                    recovery_session,
                    recovery_window,
                    BarnetCheckpointV1(
                        cursor="live",
                        live_scope=recovery_scope,
                        completed_queries=barnet_adapter.expected_live_query_keys(
                            recovery_scope
                        ),
                    ),
                )
            ]
            assert recovered[-1].complete
            assert recovered[-1].next_checkpoint.live_complete
            await recovery_session.aclose()

    asyncio.run(exercise())


def test_live_fetch_rejects_reference_mismatch_and_marks_unavailable_children() -> None:
    """The routed record must agree with the human reference at the live boundary."""
    mismatched = SUMMARY.replace(b"TCP/0001/26", b"TCP/9999/26")

    async def exercise() -> None:
        mismatch_session = _session(_BarnetMock(summary=mismatched))
        with pytest.raises(BarnetReferenceMismatchError, match="TCP/9999/26"):
            await BarnetAdapter().fetch(
                mismatch_session,
                SourceReference(
                    source_id=SourceId("barnet-idox-current"),
                    reference="TCP/0001/26",
                    locator="KEY-1",
                ),
            )
        await mismatch_session.aclose()

        unavailable_session = _session(_BarnetMock(child_unavailable=True))
        snapshot = await BarnetAdapter().fetch(
            unavailable_session,
            SourceReference(
                source_id=SourceId("barnet-idox-current"),
                reference="TCP/0001/26",
                locator="KEY-1",
            ),
        )
        assert snapshot.completeness.documents.kind == "failed"
        assert snapshot.completeness.comments.kind == "failed"
        assert len(snapshot.evidence) == 1
        await unavailable_session.aclose()

    asyncio.run(exercise())


def test_barnet_form_and_search_boundary_variants() -> None:
    """Form and result parsing rejects missing routing and count agreements."""
    with pytest.raises(BarnetParseError, match="form"):
        barnet_adapter._parse_form(b"<html></html>")
    with pytest.raises(BarnetParseError, match="_csrf"):
        barnet_adapter._parse_form(b'<form><input name="week"></form>')
    duplicate_weekly_form = WEEKLY_FORM.replace(
        b"</body>",
        b'<form action="weeklyListResults.do?action=firstPage" method="post">'
        b'<input name="_csrf" value="second"></form></body>',
    )
    for malformed in (
        WEEKLY_FORM.replace(b'method="post"', b'method="get"'),
        WEEKLY_FORM.replace(
            b"weeklyListResults.do?action=firstPage",
            b"unexpected.do",
        ),
        WEEKLY_FORM.replace(b'name="searchCriteria.ward"', b'name="otherWard"'),
        WEEKLY_FORM.replace(b'name="week"', b'name="otherWeek"'),
        WEEKLY_FORM.replace(b'value="Application"', b'value="Other"'),
        WEEKLY_FORM.replace(
            b'<input type="hidden" name="searchType" value="Application">',
            b'<input type="checkbox" name="searchType" value="Application">',
        ),
        WEEKLY_FORM.replace(
            b'<input type="radio" name="dateType" value="DC_Decided">',
            b"",
        ),
        WEEKLY_FORM.replace(b'value="DC_Decided"', b'value="DC_Validated"'),
        duplicate_weekly_form,
    ):
        with pytest.raises(BarnetParseError, match="weekly form"):
            barnet_adapter._parse_form(malformed)
    with pytest.raises(BarnetParseError, match="advanced form status options"):
        barnet_adapter._parse_advanced_form(
            ADVANCED_FORM.replace(
                b'<option value="Appeal Valid">Appeal Valid</option>',
                b"",
            )
        )
    for filtered_advanced in (
        ADVANCED_FORM.replace(
            b'name="caseAddressType" value=""', b'name="caseAddressType" value="Site"'
        ),
        ADVANCED_FORM.replace(
            b'name="searchType" value=""', b'name="searchType" value="Application"'
        ),
        ADVANCED_FORM.replace(
            b"</form>",
            b'<input name="searchCriteria.ward" value="North"></form>',
        ),
    ):
        with pytest.raises(BarnetParseError, match="advanced form neutral filters"):
            barnet_adapter._parse_advanced_form(filtered_advanced)
    for malformed in (
        b"<html></html>",
        ADVANCED_FORM.replace(b'method="post"', b'method="get"'),
        ADVANCED_FORM.replace(
            b"advancedSearchResults.do?action=firstPage",
            b"unexpected.do",
        ),
        ADVANCED_FORM.replace(b'name="searchCriteria.caseStatus"', b'name="other"'),
        ADVANCED_FORM.replace(b'name="searchType"', b'name="otherType"'),
        ADVANCED_FORM.replace(
            b'name="date(applicationReceivedStart)"',
            b'name="otherDate"',
        ),
    ):
        with pytest.raises(BarnetParseError, match="advanced form"):
            barnet_adapter._parse_advanced_form(malformed)

    weekly_form = barnet_adapter._parse_form(
        WEEKLY_FORM.replace(
            b'<option value="07/09/2026">',
            b'<option value="bad">bad</option><option value="07/09/2026">',
        )
    )
    assert barnet_adapter._weekly_queries(
        weekly_form,
        barnet_adapter.BarnetDiscoveryScope(
            start=WEEK.start,
            end=WEEK.end,
            include_open=False,
        ),
    )
    duplicate_week = WEEKLY_FORM.replace(
        b'<option value="14/09/2026">14 September</option>',
        b'<option value="14/09/2026">first</option>'
        b'<option value="14/09/2026">second</option>',
    )
    with pytest.raises(BarnetParseError, match="weekly form weeks"):
        barnet_adapter._weekly_queries(
            barnet_adapter._parse_form(duplicate_week),
            barnet_adapter.BarnetDiscoveryScope(
                start=WEEK.start,
                end=WEEK.end,
                include_open=False,
            ),
        )
    missing_week = WEEKLY_FORM.replace(
        b'<option value="14/09/2026">14 September</option>',
        b"",
    )
    with pytest.raises(BarnetParseError, match="weekly form weeks"):
        barnet_adapter._weekly_queries(
            barnet_adapter._parse_form(missing_week),
            barnet_adapter.BarnetDiscoveryScope(
                start=WEEK.start,
                end=WEEK.end,
                include_open=False,
            ),
        )

    soup = BeautifulSoup(
        '<form><input name="skip" type="submit"><select name="empty"></select>'
        '<textarea name="notes"> hello </textarea><input name="odd"></form>',
        "html.parser",
    )
    form = soup.select_one("form")
    assert form is not None
    odd = form.select_one('input[name="odd"]')
    assert odd is not None
    odd["name"] = ["not-a-string"]  # type: ignore[assignment]
    assert barnet_adapter._form_fields(form) == (
        FormField(name="empty", value=""),
        FormField(name="notes", value="hello"),
    )
    assert barnet_adapter._override_fields(form, {"extra": "value"})[-1] == FormField(
        name="extra",
        value="value",
    )

    missing_link = b'<li class="searchresult"><p>Reference: A</p></li>'
    with pytest.raises(BarnetParseError, match="summary link"):
        barnet_adapter._parse_search_page(missing_link)
    missing_locator = (
        b'<li class="searchresult"><a href="applicationDetails.do">Details</a>'
        b"<p>Reference: A</p></li>"
    )
    with pytest.raises(BarnetParseError, match="keyVal"):
        barnet_adapter._parse_search_page(missing_locator)
    missing_count = _result_page((("A", "KEY"),), count=1).replace(
        b'<div data-result-count="1"></div>',
        b"",
    )
    with pytest.raises(BarnetParseError, match="reported result count"):
        barnet_adapter._parse_search_page(missing_count)

    fallback_count = (
        b'<li class="searchresult"><a href="applicationDetails.do?keyVal=KEY">'
        b"Details</a><p>Reference: A</p></li> Displaying 1 of 1 results"
    )
    parsed = barnet_adapter._parse_search_page(fallback_count)
    assert parsed.reported == 1
    assert parsed.references[0].reference == "A"
    for range_less_pager in (
        fallback_count
        + b'<a href="pagedSearchResults.do?searchCriteria.page=bad">next</a>',
        _result_page((("A", "KEY"),), count=1, pages=2).replace(
            b'<p class="pager"><span class="showing">Showing 1-1 of 1</span></p>',
            b"",
        ),
    ):
        with pytest.raises(BarnetParseError, match="reported result count"):
            barnet_adapter._parse_search_page(range_less_pager)

    live_count = (
        b'<li class="searchresult"><a href="applicationDetails.do?keyVal=KEY">'
        b"Details</a><p>Ref. No: A</p></li> Showing 1-1 of 1"
    )
    parsed = barnet_adapter._parse_search_page(live_count)
    assert parsed.reported == 1
    assert parsed.references[0].reference == "A"
    empty = barnet_adapter._parse_search_page(b"<p>No results found</p>")
    assert empty.reported == 0
    assert empty.references == ()

    uncounted = barnet_adapter._parse_search_page(
        _uncounted_result_page((("A", "KEY"),))
    )
    assert uncounted.reported == 1
    advanced_uncounted = barnet_adapter._parse_advanced_search_page(
        _uncounted_result_page((("A", "KEY"),), current_page=""),
        page=1,
    )
    assert advanced_uncounted.reported == 1
    advanced_showing = barnet_adapter._parse_advanced_search_page(
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            current_page="",
        ),
        page=1,
    )
    assert advanced_showing.reported == 1


def test_barnet_result_count_boundaries_fail_closed() -> None:
    accepted = barnet_adapter._parse_search_page(
        _showing_result_page(
            tuple((f"A-{index}", f"KEY-{index}") for index in range(41, 46)),
            ("Showing 41-45 of 45", "Showing 41-45 of 45"),
            current_page="1",
            visible_pages=("5",),
            numbered_page=4,
        )
    )
    assert accepted.reported == 45

    range_less_multi_page = barnet_adapter._parse_search_page(
        _result_page((("A", "KEY"),), count=2)
    )
    with pytest.raises(BarnetParseError, match="displayed result range"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(cursor="live"),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Validated",
                page=1,
                row_count=0,
            ),
            search_page=range_less_multi_page,
            all_query_keys=("weekly|2026-09-14|DC_Validated",),
        )

    duplicate_identity_page = barnet_adapter._parse_search_page(
        _showing_result_page(
            (("A", "KEY"), ("A", "KEY")),
            ("Showing 1-2 of 2",),
        )
    )
    with pytest.raises(BarnetParseError, match="duplicate search result identity"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(cursor="live", tracks_locators=True),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Validated",
                page=1,
                row_count=0,
            ),
            search_page=duplicate_identity_page,
            all_query_keys=("weekly|2026-09-14|DC_Validated",),
        )

    repeated_second_page = barnet_adapter._parse_search_page(
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 2-2 of 2",),
            current_page="2",
        )
    )
    with pytest.raises(BarnetParseError, match="duplicate search result identity"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(
                cursor="live",
                active_query="weekly|2026-09-14|DC_Validated",
                next_page=2,
                query_row_count=1,
                active_query_references=("A",),
                seen_references=("A",),
                seen_locators=("KEY",),
                tracks_locators=True,
            ),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Validated",
                page=2,
                row_count=1,
            ),
            search_page=repeated_second_page,
            all_query_keys=("weekly|2026-09-14|DC_Validated",),
        )

    changed_total = barnet_adapter._parse_search_page(
        _showing_result_page(
            tuple((f"A-{index}", f"KEY-{index}") for index in range(11, 16)),
            ("Showing 11-15 of 15",),
            current_page="2",
        )
    )
    with pytest.raises(BarnetCountMismatchError, match="expected 20 actual 15"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(
                cursor="live",
                active_query="weekly|2026-09-14|DC_Validated",
                next_page=2,
                query_row_count=10,
                query_reported_count=20,
                active_query_references=tuple(f"A-{index}" for index in range(1, 11)),
                seen_references=tuple(f"A-{index}" for index in range(1, 11)),
                seen_locators=tuple(f"KEY-{index}" for index in range(1, 11)),
                tracks_locators=True,
            ),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Validated",
                page=2,
                row_count=10,
            ),
            search_page=changed_total,
            all_query_keys=("weekly|2026-09-14|DC_Validated",),
        )

    legacy_active = BarnetCheckpointV1(
        cursor="live",
        active_query="weekly|2026-09-14|DC_Validated",
        next_page=2,
        query_row_count=1,
        seen_references=("A",),
    )
    active_page = barnet_adapter._ActivePage(
        query_key="weekly|2026-09-14|DC_Validated",
        page=2,
        row_count=1,
    )
    assert barnet_adapter._active_query_references(legacy_active, active_page) == ("A",)
    with pytest.raises(BarnetCheckpointError, match="active query identities"):
        barnet_adapter._active_query_references(
            legacy_active.model_copy(update={"active_query": "other"}),
            active_page,
        )

    resumable_first_page = barnet_adapter._parse_search_page(
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 2",),
        )
    )
    with pytest.raises(BarnetParseError, match="resumed search result identity"):
        barnet_adapter._restore_query_progress(
            legacy_active.model_copy(update={"seen_references": ("B",)}),
            active_page.query_key,
            (resumable_first_page,),
        )
    with pytest.raises(BarnetParseError, match="resumed search result identity"):
        barnet_adapter._restore_query_progress(
            legacy_active,
            active_page.query_key,
            (resumable_first_page.model_copy(update={"displayed_range": (2, 2)}),),
        )
    with pytest.raises(BarnetCountMismatchError, match="expected 3 actual 2"):
        barnet_adapter._restore_query_progress(
            legacy_active.model_copy(update={"query_reported_count": 3}),
            active_page.query_key,
            (resumable_first_page,),
        )

    locator_checkpoint = legacy_active.model_copy(
        update={
            "active_query_references": ("A",),
            "seen_locators": ("KEY-OLD",),
            "tracks_locators": True,
        }
    )
    changed_locator_page = barnet_adapter._parse_search_page(
        _showing_result_page(
            (("A", "KEY-NEW"),),
            ("Showing 1-1 of 2",),
        )
    )
    with pytest.raises(BarnetParseError, match="resumed search result identity"):
        barnet_adapter._restore_query_progress(
            locator_checkpoint,
            active_page.query_key,
            (changed_locator_page,),
        )

    page_three_checkpoint = BarnetCheckpointV1(
        cursor="live",
        active_query=active_page.query_key,
        next_page=3,
        query_row_count=2,
        query_reported_count=3,
        active_query_references=("A", "B"),
        seen_references=("A", "B"),
        seen_locators=("KEY-A", "KEY-B"),
        tracks_locators=True,
    )
    prior_pages = (
        barnet_adapter._parse_search_page(
            _showing_result_page(
                (("A", "KEY-A"),),
                ("Showing 1-1 of 3",),
            )
        ),
        barnet_adapter._parse_search_page(
            _showing_result_page(
                (("C", "KEY-C"),),
                ("Showing 2-2 of 3",),
                current_page="2",
            )
        ),
    )
    with pytest.raises(BarnetParseError, match="resumed search result identity"):
        barnet_adapter._restore_query_progress(
            page_three_checkpoint,
            active_page.query_key,
            prior_pages,
        )

    conflicting_identity = barnet_adapter._parse_search_page(
        _result_page((("A", "OTHER-KEY"),), count=1)
    )
    with pytest.raises(BarnetParseError, match="search result identity"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(
                cursor="live",
                seen_references=("A",),
                seen_locators=("KEY",),
            ),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Decided",
                page=1,
                row_count=0,
            ),
            search_page=conflicting_identity,
            all_query_keys=("weekly|2026-09-14|DC_Decided",),
        )

    legacy_identity = BarnetCheckpointV1(
        cursor="live",
        seen_references=("A",),
    )
    reconciled, fresh, _complete = barnet_adapter._advance_checkpoint(
        legacy_identity,
        active_page=barnet_adapter._ActivePage(
            query_key="weekly|2026-09-14|DC_Decided",
            page=1,
            row_count=0,
        ),
        search_page=barnet_adapter._parse_search_page(
            _result_page((("A", "KEY"),), count=1)
        ),
        all_query_keys=("weekly|2026-09-14|DC_Decided",),
    )
    assert fresh[0].locator == "KEY"
    assert reconciled.seen_locators == ("KEY",)

    for corrupt_identity in (
        BarnetCheckpointV1(cursor="live", seen_locators=("KEY",)),
        BarnetCheckpointV1(
            cursor="live",
            seen_references=("A", "A"),
        ),
    ):
        with pytest.raises(BarnetCheckpointError, match="identities"):
            barnet_adapter._reconcile_search_identities(corrupt_identity, ())

    with pytest.raises(BarnetParseError, match="search result identity"):
        barnet_adapter._reconcile_search_identities(
            BarnetCheckpointV1(cursor="live"),
            (
                SourceReference(
                    source_id=SourceId("barnet-idox-current"),
                    reference="A",
                ),
            ),
        )

    replayed_first_page = barnet_adapter._parse_search_page(
        _showing_result_page(
            tuple((f"A-{index}", f"KEY-{index}") for index in range(1, 11)),
            ("Showing 1-10 of 20",),
            current_page="1",
            numbered_page=2,
        )
    )
    with pytest.raises(BarnetParseError, match="displayed result range"):
        barnet_adapter._advance_checkpoint(
            BarnetCheckpointV1(
                cursor="live",
                seen_references=tuple(f"A-{index}" for index in range(1, 11)),
            ),
            active_page=barnet_adapter._ActivePage(
                query_key="weekly|2026-09-14|DC_Validated",
                page=2,
                row_count=10,
            ),
            search_page=replayed_first_page,
            all_query_keys=("weekly|2026-09-14|DC_Validated",),
        )

    with pytest.raises(BarnetParseError, match="reported result count"):
        barnet_adapter._parse_search_page(
            b'<div data-result-count="1"></div>'
            + _showing_result_page(
                (("A", "KEY"),),
                ("Showing 1-1 of 2",),
                numbered_page=2,
            )
        )
    with pytest.raises(BarnetParseError, match="reported result count"):
        barnet_adapter._parse_search_page(
            b'<div data-result-count="1"></div>'
            + _showing_result_page(
                (("A", "KEY"),),
                ("Showing 1-1 of 1", "Showing 1-1 of 2"),
            )
        )

    invalid_showing_pages = (
        _showing_result_page((("A", "KEY"),), ("Showing one-1 of 1",)),
        _showing_result_page((("A", "KEY"),), ("Showing 2-1 of 1",)),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1", "Showing 1-1 of 2"),
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            numbered_page=2,
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            current_page="later",
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            current_page=None,
            numbered_page=2,
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
        )
        + b'<a href="pagedSearchResults.do?action=next">next</a>',
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
        )
        + b'<a href="pagedSearchResults.do?action=page&amp;searchCriteria.page=0">'
        b"zero</a>",
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            visible_pages=("1", "2"),
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            visible_pages=("later",),
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            visible_pages=("1",),
            capacity=None,
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            visible_pages=("1",),
            capacity="many",
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 1-1 of 1",),
            visible_pages=("1",),
            capacity="0",
        ),
        _showing_result_page(
            (("A", "KEY"),),
            ("Showing 11-11 of 11",),
            visible_pages=("1",),
        ),
    )
    for body in invalid_showing_pages:
        with pytest.raises(BarnetParseError, match="reported result count"):
            barnet_adapter._parse_search_page(body)

    ambiguous_uncounted = (
        _uncounted_result_page(()),
        _uncounted_result_page((("A", "KEY"),), capacity=None),
        _uncounted_result_page((("A", "KEY"),), capacity="many"),
        _uncounted_result_page((("A", "KEY"),), capacity="0"),
        _uncounted_result_page((("A", "KEY"),), capacity="1"),
        _uncounted_result_page((("A", "KEY"),), numbered_page=2),
        _uncounted_result_page((("A", "KEY"),), repeated_page_action=True),
        _uncounted_result_page((("A", "KEY"),), current_page="2"),
        _uncounted_result_page((("A", "KEY"),), current_page=None),
    )
    for body in ambiguous_uncounted:
        with pytest.raises(BarnetParseError, match="reported result count"):
            barnet_adapter._parse_search_page(body)


def test_barnet_parses_live_div_comment_layouts() -> None:
    public, public_state = barnet_adapter._parse_comments(
        b'<h2>Public Comments (1)</h2><div id="comments">'
        b'<div class="comment"><h1><span class="consultationName">Redacted</span>'
        b'<span class="consultationAddress">Redacted</span>'
        b'<span class="consultationStance">Neutral</span></h1>'
        b'<div class="comment-wrapper"><h2>Comment submitted date: 28/08/2026</h2>'
        b'<div class="comment-text"><p>Support recorded.<br></p></div>'
        b'<div class="comment-report-button"></div></div></div></div>',
        "public",
        ("public comments", "neighbour comments"),
    )
    assert [comment.text for comment in public] == ["Support recorded."]
    assert public[0].comment_id == "public-1"
    assert public_state.kind == "complete"

    consultation_cards = (
        b'<div id="comments">'
        b'<div class="comment"><h1>Trees &amp; Landscape</h1>'
        b'<div class="commentText"><h2>Consultation Date: 24/08/2026</h2></div>'
        b'</div><div class="comment"><h1>Ecology</h1>'
        b'<div class="commentText"><h2>Consultation Date: 25/08/2026</h2></div>'
        b"</div></div>"
    )
    consultee, consultee_state = barnet_adapter._parse_comments(
        b"<h2>Consultee Comments (0)</h2>" + consultation_cards,
        "consultee",
        ("consultee comments", "consultee responses"),
    )
    assert consultee == ()
    assert consultee_state.kind == "empty"

    consultee, consultee_state = barnet_adapter._parse_comments(
        b"<h2>Consultee Comments (1)</h2>" + consultation_cards,
        "consultee",
        ("consultee comments", "consultee responses"),
    )
    assert consultee == ()
    assert consultee_state.kind == "unavailable"

    for malformed_card in (
        b'<div id="comments"><div class="comment"></div></div>',
        (
            b'<div id="comments"><div class="comment">'
            b'<div class="comment-text"></div></div></div>'
        ),
    ):
        with pytest.raises(BarnetParseError, match="public comment text"):
            barnet_adapter._parse_comments(
                b"<h2>Public Comments (1)</h2>" + malformed_card,
                "public",
                ("public comments", "neighbour comments"),
            )


def test_barnet_advanced_detail_redirect_boundaries() -> None:
    detail = (
        b'<a href="applicationDetails.do?keyVal=KEY">Details</a>'
        b'<table id="simpleDetailsTable"><tr><th>Reference</th><td>A</td></tr>'
        b"</table>"
    )
    parsed = barnet_adapter._parse_advanced_search_page(detail, page=1)
    assert parsed.reported == 1
    assert parsed.references[0].locator == "KEY"
    with pytest.raises(BarnetParseError, match="advanced detail redirect"):
        barnet_adapter._parse_advanced_search_page(detail, page=2)

    invalid_redirects = (
        detail + b'<table id="simpleDetailsTable"></table>',
        detail + b'<li class="searchresult"></li>',
        detail + b"<p>No results found</p>",
        detail.replace(b"?keyVal=KEY", b""),
        detail.replace(
            b"</table>",
            b'</table><a href="applicationDetails.do?keyVal=OTHER">Other</a>',
        ),
    )
    for body in invalid_redirects:
        with pytest.raises(BarnetParseError, match="advanced detail"):
            barnet_adapter._parse_advanced_search_page(body, page=1)


def test_barnet_detail_boundary_variants() -> None:
    """Detail sections distinguish empty, unavailable, malformed, and complete."""
    with pytest.raises(BarnetParseError, match="simpleDetailsTable"):
        barnet_adapter._parse_summary(b"<html></html>")
    with pytest.raises(BarnetParseError, match="summary labelled values"):
        barnet_adapter._parse_summary(
            b'<table id="simpleDetailsTable"><tr><td>orphan</td></tr></table>'
        )

    documents, state = barnet_adapter._parse_documents(
        b"<p>Information is not available</p>"
    )
    assert documents == ()
    assert state.kind == "unavailable"
    documents, state = barnet_adapter._parse_documents(
        b'<div data-section="documents" data-count="0"></div>'
    )
    assert documents == ()
    assert state.kind == "empty"
    with pytest.raises(BarnetParseError, match="metadata link"):
        barnet_adapter._parse_documents(
            b'<div data-section="documents" data-count="1"></div>'
            b'<table summary="Documents"><tbody><tr><td>none</td></tr></tbody></table>'
        )
    documents, state = barnet_adapter._parse_documents(
        b'<div data-section="documents" data-count="1"></div>'
        b'<table summary="Documents"><tr><td><a href="file?id=1">Download</a>'
        b"</td></tr></table>"
    )
    assert documents[0].title == "Download"
    assert documents[0].published_date is None
    assert state.kind == "complete"
    documents, state = barnet_adapter._parse_documents(
        b'<div data-section="documents" data-count="0"></div>'
        b'<table summary="Documents"><tr><th>Heading</th></tr></table>'
    )
    assert documents == ()
    assert state.kind == "empty"
    with pytest.raises(BarnetCountMismatchError, match="expected 2 actual 1"):
        barnet_adapter._parse_documents(
            b'<div data-section="documents" data-count="2"></div>'
            b'<table summary="Documents"><tr><td><a href="file?id=1">One</a>'
            b"</td></tr></table>"
        )

    comments, state = barnet_adapter._parse_comments(
        b"<p>Section is not available</p>",
        "public",
        ("public comments",),
    )
    assert comments == ()
    assert state.kind == "unavailable"
    comments, state = barnet_adapter._parse_comments(
        b'<div data-section="public comments" data-count="0"></div>',
        "public",
        ("public comments",),
    )
    assert comments == ()
    assert state.kind == "empty"
    with pytest.raises(BarnetParseError, match="comment text"):
        barnet_adapter._parse_comments(
            b'<div data-section="public comments" data-count="1"></div>'
            b'<table summary="Public Comments"><thead><tr><th>Other</th></tr></thead>'
            b"<tbody><tr><td>none</td></tr></tbody></table>",
            "public",
            ("public comments",),
        )
    comments, state = barnet_adapter._parse_comments(
        b'<div data-section="public comments" data-count="1"></div>'
        b'<table summary="Other"><tr><td>ignored</td></tr></table>'
        b'<table summary="Public Comments"><thead><tr><th>Comment</th></tr></thead>'
        b"<tbody><tr><td>Recorded</td></tr></tbody></table>",
        "public",
        ("public comments",),
    )
    assert comments[0].comment_id == "public-1"
    assert state.kind == "complete"
    combined = barnet_adapter._combined_comment_state(
        EmptySection(),
        UnavailableSection(reason="not exposed"),
        0,
    )
    assert combined.kind == "unavailable"


def test_barnet_label_count_and_date_boundary_variants() -> None:
    """Label, displayed-count, and date fallbacks have explicit failures."""
    soup = BeautifulSoup(
        '<div data-section="other" data-count="9"></div>'
        '<div data-section="documents" data-count="2"></div>',
        "html.parser",
    )
    assert barnet_adapter._section_count(soup, ("documents",)) == 2
    text_count = BeautifulSoup("<h2>Neighbour comments (3)</h2>", "html.parser")
    assert (
        barnet_adapter._section_count(
            text_count,
            ("public comments", "neighbour comments"),
        )
        == 3
    )
    with pytest.raises(BarnetParseError, match="displayed count"):
        barnet_adapter._section_count(
            BeautifulSoup("<p>none</p>", "html.parser"),
            ("documents",),
        )

    prefix = BeautifulSoup("<p>Reference: TCP/0001/26</p>", "html.parser").p
    assert prefix is not None
    assert barnet_adapter._labelled_value(prefix, "reference") == "TCP/0001/26"
    with pytest.raises(BarnetParseError, match="labelled missing"):
        barnet_adapter._labelled_value(prefix, "missing")
    with pytest.raises(BarnetParseError, match="missing/absent"):
        barnet_adapter._labelled_value_any(prefix, "missing", "absent")
    with pytest.raises(BarnetParseError, match="summary proposal"):
        barnet_adapter._required_field({}, "proposal")
    assert barnet_adapter._optional_date({}, "received date") is None
    with pytest.raises(BarnetParseError, match="date received date"):
        barnet_adapter._optional_date({"received date": "not-a-date"}, "received date")
    assert barnet_adapter._parse_date(None) is None
    assert barnet_adapter._parse_date("2026-09-14") == date(2026, 9, 14)
    assert barnet_adapter._parse_date("14 September 2026") == date(2026, 9, 14)
    assert barnet_adapter._parse_date("14 Sep 2026") == date(2026, 9, 14)
    assert barnet_adapter._parse_date("not-a-date") is None
    with pytest.raises(BarnetParseError, match="did not match"):
        barnet_adapter._required("value", "missing")
