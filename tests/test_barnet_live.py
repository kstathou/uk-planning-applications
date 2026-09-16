# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: C901, E501, EM102, PERF401, PLR0911, PLR0912, PLR0913, PLR2004, PT012, SLF001, TRY003

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
    BarnetOpenEnumerationUnsupportedError,
    BarnetOpenListLimitError,
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
  <input type="hidden" name="dateType" value="DC_Validated">
  <input type="hidden" name="searchType" value="Weekly List">
  <input type="submit" name="submit" value="Search">
</form></body></html>
"""

CURRENT_FORM = b"""
<!doctype html><html><body>
<form action="currentListResults.do?action=firstPage" method="post">
  <input type="hidden" name="_csrf" value="sanitised-current-csrf">
  <input type="hidden" name="searchCriteria.ward" value="">
  <select name="dateType"><option value="DC_Validated" selected>Validated</option></select>
  <input type="hidden" name="searchType" value="Current List">
</form></body></html>
"""

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
    pagination = "".join(
        (
            '<a href="pagedSearchResults.do?action=page&amp;'
            f'searchCriteria.page={page}">{page}</a>'
        )
        for page in range(1, pages + 1)
    )
    return (
        "<!doctype html><html><body>"
        f'<div data-result-count="{count}"></div><ul>{rows}</ul>{pagination}'
        "</body></html>"
    ).encode()


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
    ) -> None:
        self.multi_page = multi_page
        self.count_mismatch = count_mismatch
        self.open_message = open_message
        self.child_failure = child_failure
        self.child_unavailable = child_unavailable
        self.summary = summary
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
            assert submitted["searchType"] == "Weekly List"
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
        if path.endswith("/pagedSearchResults.do"):
            return httpx.Response(
                200,
                content=_result_page(
                    (("TCP/0003/26", "KEY-3"),),
                    count=99 if self.count_mismatch else 3,
                    pages=2,
                ),
            )
        if path.endswith("/search.do") and action == "currentList":
            return httpx.Response(200, content=CURRENT_FORM)
        if path.endswith("/currentListResults.do"):
            submitted = dict(fields)
            assert submitted == {
                "_csrf": "sanitised-current-csrf",
                "searchCriteria.ward": "",
                "dateType": "DC_Validated",
                "searchType": "Current List",
            }
            return httpx.Response(200, content=self.open_message)
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
    assert first.next_checkpoint.active_query == "14/09/2026|DC_Validated"
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


def test_live_discovery_rejects_count_mismatch_and_open_cap() -> None:
    """Displayed totals and the current-list cap cannot become completeness."""
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

    open_mock = _BarnetMock()
    open_session = _session(open_mock)

    async def open_cap() -> None:
        with pytest.raises(BarnetOpenListLimitError, match="Too many results"):
            async for _batch in BarnetAdapter().discover(
                open_session,
                WEEK_WITH_OPEN,
                None,
            ):
                pass
        await open_session.aclose()

    asyncio.run(open_cap())
    assert any(
        path.endswith("/currentListResults.do") for _, path, _ in open_mock.requests
    )

    unsupported_session = _session(_BarnetMock(open_message=b"unexpected success"))

    async def unsupported() -> None:
        with pytest.raises(BarnetOpenEnumerationUnsupportedError):
            async for _batch in BarnetAdapter().discover(
                unsupported_session,
                WEEK_WITH_OPEN,
                None,
            ):
                pass
        await unsupported_session.aclose()

    asyncio.run(unsupported())


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
    """Terminal, stale, and empty-window checkpoints never imply hidden work."""
    adapter = BarnetAdapter()

    async def exercise() -> None:
        terminal_session = _session(_BarnetMock())
        terminal = [
            batch
            async for batch in adapter.discover(
                terminal_session,
                WEEK,
                BarnetCheckpointV1(cursor="live", live_complete=True),
            )
        ]
        assert len(terminal) == 1
        assert terminal[0].complete
        assert terminal_session.requested_urls == ()
        await terminal_session.aclose()

        stale_session = _session(_BarnetMock())
        with pytest.raises(BarnetCheckpointError, match="unavailable"):
            async for _batch in adapter.discover(
                stale_session,
                WEEK,
                BarnetCheckpointV1(
                    cursor="live",
                    active_query="01/01/2000|DC_Validated",
                ),
            ):
                pass
        await stale_session.aclose()

        outside = DiscoveryWindow(
            start=date(2027, 1, 4),
            end=date(2027, 1, 10),
            include_open=False,
        )
        empty_session = _session(_BarnetMock())
        empty = [
            batch async for batch in adapter.discover(empty_session, outside, None)
        ]
        assert len(empty) == 1
        assert empty[0].complete
        assert empty[0].references == ()
        await empty_session.aclose()

        open_session = _session(_BarnetMock())
        yielded = []
        with pytest.raises(BarnetOpenListLimitError):
            async for batch in adapter.discover(
                open_session,
                outside.model_copy(update={"include_open": True}),
                None,
            ):
                yielded.append(batch)
        assert len(yielded) == 1
        assert not yielded[0].complete
        await open_session.aclose()

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
        b'<a href="pagedSearchResults.do?searchCriteria.page=bad">next</a>'
    )
    parsed = barnet_adapter._parse_search_page(fallback_count)
    assert parsed.reported == 1
    assert parsed.references[0].reference == "A"


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
