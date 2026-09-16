# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: C901, D103, PLR2004, SLF001

"""Arun live discovery and qualification contracts."""

import asyncio
import gzip
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import HttpUrl

import yimby.authorities.arun.adapter as arun
from yimby.authorities.arun import ARUN_PACKAGE
from yimby.domain import (
    ApplicationId,
    AuthorityId,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    RetainedNativeRecord,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceReference,
    TransportMode,
    UnavailableSection,
)
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore
from yimby.transport import FormField, PortalRequest, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path
    from types import ModuleType

_SEARCH_DIGEST = EvidenceDigest("0" * 64)


def _search_form() -> bytes:
    return b"""
    <form method="post" name="OcellaPlanningSearch" action="planningSearch">
      <input name="reference" value="stale">
      <input name="location" value="stale">
      <input name="OcellaPlanningSearch.postcode" value="stale">
      <select name="area"><option value=""></option>
        <option value="BR">BOGNOR REGIS</option></select>
      <input name="applicant" value="stale">
      <input name="agent" value="stale">
      <input type="checkbox" name="undecided" value="Y">
      <select name="type"><option value=""></option>
        <option value="PL">Planning Application</option></select>
      <input name="receivedFrom" value="">
      <input name="receivedTo" value="">
      <input name="decidedFrom" value="">
      <input name="decidedTo" value="">
      <input type="submit" name="action" value="Search">
      <input type="submit" name="action" value="Reset">
    </form>
    """


def _partial_results(query_fields: str) -> bytes:
    return f"""
    <table><tr><th>Reference</th><th>Location</th><th>Proposal</th><th>Status</th></tr>
      <tr><td><a href="planningDetails?reference=BR/1/26/PL&amp;from=planningSearch">
        BR/1/26/PL</a></td><td>Site</td><td>Proposal</td><td>Undecided</td></tr>
    </table>
    <strong>First 1 results shown, there are 2 in total</strong>
    <form method="post" action="planningSearch">
      <input type="hidden" name="action" value="Search">
      <input type="hidden" name="showall" value="showall">
      {query_fields}
      <input type="submit" value="Show all results">
    </form>
    """.encode()


def _complete_results(references: tuple[str, ...]) -> bytes:
    rows = "".join(
        '<tr><td><a href="planningDetails?reference='
        f'{reference}&amp;from=planningSearch">{reference}</a></td>'
        "<td>Site</td><td>Proposal</td><td>Undecided</td></tr>"
        for reference in references
    )
    return (
        '<form method="post" name="search" action="planningSearch">'
        '<input type="submit" name="BackToSearch" value="Back to Search page">'
        "</form><table><tr><th>Reference</th><th>Location</th>"
        f"<th>Proposal</th><th>Status</th></tr>{rows}</table>"
    ).encode()


def _empty_results() -> bytes:
    return _search_form().replace(
        b"</form>",
        (
            b"<strong>"
            b'<span style="color:maroon">'
            b"No applications found for entered search criteria"
            b"</span></strong></form>"
        ),
        1,
    )


def _detail_with_documents(reference: str) -> bytes:
    return f"""
    <table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Proposal</th><td>Build one home</td></tr>
      <tr><th>Status</th><td>Undecided</td></tr>
      <tr><th>Parish</th><td>Bognor Regis</td></tr>
      <tr><th>Received</th><td>01-09-26</td></tr>
      <tr><th>Validated</th><td>02-09-26</td></tr>
      <tr><th>Decision By</th><td>01-11-26</td></tr>
      <tr><th>Comment By</th><td>30-09-26</td></tr>
      <tr><th>Target Cmte</th><td>15-10-26</td></tr>
    </table>
    <form method="post" action="showDocuments?reference={reference}&amp;module=pl">
      <input type="submit" name="ViewDocuments" value="View Documents">
    </form>
    """.encode()


def _document_index() -> bytes:
    return (
        b'<form method="post" action="showDocuments?reference=BR/1/26/PL'
        b'&amp;module=pl&amp;filterBy=TYPE">'
        b"""
      <select name="selectedtype"><option value="" selected>All</option></select>
    </form>
    <table>
      <tr><th>Type</th><th></th><th>Date</th><th></th><th>Description</th></tr>
      <tr>
        <td><a href="viewDocument?file=decision-1.pdf&amp;module=pl">Decision</a></td>
        <td></td><td>15/09/2026</td><td></td><td>Decision notice</td>
      </tr>
      <tr>
        <td><a href="viewDocument?file=plan-1.pdf&amp;module=pl">Plan</a></td>
        <td></td><td>14 Sep 2026</td><td></td><td></td>
      </tr>
    </table>
    """
    )


def _document_filter() -> bytes:
    return (
        b'<select name="selectedtype"><option value="" selected>All</option></select>'
    )


def _empty_document_index() -> bytes:
    return (
        _document_filter()
        + b"<strong>Documents</strong><table><tr><td>"
        + b"There are no documents for this section"
        + b"</td></tr></table>"
    )


def _headerless_document_index() -> bytes:
    return (
        b"""
    <table><tr><td>
      <form method="post" action="showDocuments?reference=FG/95/26/HH"""
        b"""&amp;module=pl&amp;filterBy=TYPE">
        <select name="selectedtype"><option value=""></option></select>
      </form>
    </td></tr></table>
    <strong>Documents</strong>
    <table>
      <tr><td><a href="viewDocument?file=one.pdf&amp;module=pl">Application</a></td>
      <td></td><td>15-09-26</td><td></td>
      <td>Application Form - Without Personal Data</td></tr>
    </table>
    """
    )


class _Session:
    def __init__(
        self,
        responder: "Callable[[PortalRequest], bytes]",
        *,
        mode: TransportMode = TransportMode.LIVE,
    ) -> None:
        self.responder = responder
        self.requests: list[PortalRequest] = []
        self._mode = mode
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
        return self._mode

    async def aclose(self) -> None:
        self.closed = True


class _DiscoveryResponder:
    references = ("BR/1/26/PL", "BR/2/26/PL")

    def __call__(self, request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        values = {field.name: field.value for field in request.form}
        if values.get("showall") == "showall":
            return _complete_results(self.references)
        if values.get("receivedFrom") == "18-08-26" and values.get("undecided") == "":
            fields = "".join(
                f'<input type="hidden" name="{name}" value="{value}">'
                for name, value in values.items()
                if name != "action"
            )
            return _partial_results(fields)
        if values.get("receivedFrom") == "01-01-01":
            return _complete_results((self.references[0],))
        return _empty_results()


class _QualificationResponder(_DiscoveryResponder):
    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if "planningDetails" in url:
            reference = parse_qs(urlsplit(url).query)["reference"][0]
            return _detail_with_documents(reference)
        if "showDocuments" in url:
            return _document_index()
        return super().__call__(request)


def _qualification_module() -> "ModuleType":
    path = __file__.rsplit("/tests/", maxsplit=1)[0]
    module_path = f"{path}/scripts/qualify_arun.py"
    name = "_test_qualify_arun"
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _assert_first_receipt_request_is_bound(receipt: dict[str, object]) -> None:
    query_inventory = cast("list[dict[str, object]]", receipt["query_inventory"])
    initial = cast("dict[str, object]", query_inventory[0]["initial_request"])
    expanded = cast("dict[str, object]", query_inventory[0]["expanded_request"])
    assert initial["method"] == "POST"
    assert expanded["method"] == "POST"
    form = cast("list[dict[str, str]]", initial["form"])
    assert [field["name"] for field in form] == [
        "reference",
        "location",
        "OcellaPlanningSearch.postcode",
        "area",
        "applicant",
        "agent",
        "undecided",
        "type",
        "receivedFrom",
        "receivedTo",
        "decidedFrom",
        "decidedTo",
        "action",
    ]


def _assert_sessions_closed_and_rerun_empty(sessions: list[_Session]) -> None:
    assert all(session.closed for session in sessions)
    assert sessions[1].requested_urls == ()


async def _batches(
    adapter: arun.ArunAdapter,
    session: _Session,
    window: DiscoveryWindow,
    checkpoint: arun.ArunCheckpointV1 | None,
) -> tuple[DiscoveryBatch[arun.ArunCheckpointV1], ...]:
    return tuple(
        [batch async for batch in adapter.discover(session, window, checkpoint)]
    )


def test_arun_canonical_query_plan_is_complete_and_non_overlapping() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )

    plan = arun._canonical_query_plan(scope)

    assert len(plan) == 60
    assert isinstance(plan[0], arun.ArunReceivedQuery)
    assert plan[0].key == "received|2026-08-18|2026-09-16"
    assert isinstance(plan[1], arun.ArunDecidedQuery)
    assert plan[1].key == "decided|2026-08-18|2026-09-16"

    open_queries = plan[2:]
    assert all(isinstance(query, arun.ArunOpenReceivedQuery) for query in open_queries)
    assert open_queries[0].key == "open-received|1948-01-01|1999-12-31"
    assert open_queries[1].key == "open-received|2000-01-01|2000-12-31"
    assert open_queries[24].key == "open-received|2023-01-01|2023-12-31"
    assert open_queries[25].key == "open-received|2024-01-01|2024-01-31"
    assert open_queries[-1].key == "open-received|2026-09-01|2026-09-16"
    assert all(
        left.end + timedelta(days=1) == right.start
        for left, right in pairwise(open_queries)
    )


def test_arun_plan_without_open_contains_only_the_bounded_first_pass() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )

    plan = arun._canonical_query_plan(scope)

    assert [query.kind for query in plan] == ["received", "decided"]


def test_arun_open_plan_stops_at_a_pre_2024_scope_end() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2020, 8, 18),
        end=date(2020, 9, 16),
        include_open=True,
    )

    plan = arun._canonical_query_plan(scope)

    open_queries = cast("tuple[arun.ArunOpenReceivedQuery, ...]", plan[2:])
    assert open_queries[0] == arun.ArunOpenReceivedQuery(
        start=date(1948, 1, 1),
        end=date(1999, 12, 31),
    )
    assert open_queries[-1] == arun.ArunOpenReceivedQuery(
        start=date(2020, 1, 1),
        end=date(2020, 9, 16),
    )
    assert all(query.end <= scope.end for query in open_queries)

    assert (
        len(
            arun._canonical_query_plan(
                scope.model_copy(update={"end": date(1947, 12, 31)})
            )
        )
        == 2
    )
    historical = arun._canonical_query_plan(
        scope.model_copy(update={"end": date(1990, 9, 16)})
    )
    assert historical[-1].end == date(1990, 9, 16)


def test_arun_search_requests_replay_the_exact_portal_controls() -> None:
    form = arun._parse_search_form(_search_form())
    queries = (
        arun.ArunReceivedQuery(start=date(2026, 8, 18), end=date(2026, 9, 16)),
        arun.ArunDecidedQuery(start=date(2026, 8, 18), end=date(2026, 9, 16)),
        arun.ArunOpenReceivedQuery(start=date(2024, 1, 1), end=date(2024, 1, 31)),
    )

    requests = tuple(arun._initial_search_request(form, query) for query in queries)

    assert all(request.method == RequestMethod.POST for request in requests)
    assert all(str(request.url).rstrip("/") == arun._SEARCH_URL for request in requests)
    received, decided, open_received = (
        {field.name: field.value for field in request.form} for request in requests
    )
    assert received == {
        "reference": "",
        "location": "",
        "OcellaPlanningSearch.postcode": "",
        "area": "",
        "applicant": "",
        "agent": "",
        "undecided": "",
        "type": "",
        "receivedFrom": "18-08-26",
        "receivedTo": "16-09-26",
        "decidedFrom": "",
        "decidedTo": "",
        "action": "Search",
    }
    assert decided["decidedFrom"] == "18-08-26"
    assert decided["decidedTo"] == "16-09-26"
    assert decided["receivedFrom"] == ""
    assert open_received["undecided"] == "Y"
    assert open_received["receivedFrom"] == "01-01-24"
    assert open_received["receivedTo"] == "31-01-24"


def test_arun_show_all_form_must_exactly_replay_the_active_query() -> None:
    query = arun.ArunReceivedQuery(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
    )
    fields = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in {
            "reference": "",
            "location": "",
            "OcellaPlanningSearch.postcode": "",
            "area": "",
            "applicant": "",
            "agent": "",
            "undecided": "",
            "type": "",
            "receivedFrom": "18-08-26",
            "receivedTo": "16-09-26",
            "decidedFrom": "",
            "decidedTo": "",
        }.items()
    )

    results = arun._parse_search_results(_partial_results(fields))
    request = arun._show_all_request(results.show_all_form, query)

    assert results.reported == 2
    assert [reference.reference for reference in results.references] == ["BR/1/26/PL"]
    assert request.method == RequestMethod.POST
    assert [(field.name, field.value) for field in request.form][:2] == [
        ("action", "Search"),
        ("showall", "showall"),
    ]

    wrong = fields.replace("18-08-26", "19-08-26")
    wrong_results = arun._parse_search_results(_partial_results(wrong))
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(wrong_results.show_all_form, query)
    assert results.show_all_form is not None
    cross_host = results.show_all_form.model_copy(
        update={"action": HttpUrl("https://elsewhere.invalid/planningSearch")}
    )
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(cross_host, query)


def test_arun_result_parser_fails_closed_on_the_portal_cap() -> None:
    with pytest.raises(arun.ArunResultCapError):
        arun._parse_search_results(
            b"Entered search criteria will retrieve more than 200 results"
        )
    with pytest.raises(arun.ArunResultCapError):
        arun._parse_search_results(
            _complete_results(tuple(f"REF/{index}" for index in range(200)))
        )

    empty = arun._parse_search_results(_empty_results())
    assert empty.reported == 0
    assert empty.references == ()
    with pytest.raises(arun.ArunParseError, match="reported result count"):
        arun._parse_search_results(
            _empty_results().replace(b'name="receivedTo"', b'name="wrong"')
        )

    duplicate = (
        b"<table><tr><th>Reference</th><th>Location</th><th>Proposal</th>"
        b'<th>Status</th></tr><tr><td><a href="planningDetails?reference=A">A</a>'
        b"</td><td>Site</td><td>Proposal</td><td>Open</td></tr>"
        b'<tr><td><a href="planningDetails?reference=A">A</a></td>'
        b"<td>Site</td><td>Proposal</td><td>Open</td></tr></table>"
        b"<strong>First 20 results shown, there are 2 in total</strong>"
    )
    with pytest.raises(arun.ArunParseError, match="duplicate result reference"):
        arun._parse_search_results(duplicate)
    with pytest.raises(arun.ArunParseError, match="result table"):
        arun._parse_search_results(
            b'<a href="planningDetails?reference=A">A</a><p>1 result</p>'
        )
    with pytest.raises(arun.ArunParseError, match="reported result count"):
        arun._parse_search_results(b"The archive contains 7 records")
    with pytest.raises(arun.ArunParseError, match="reported result count"):
        arun._parse_search_results(
            b"<strong>First 20 results shown, there are 2 in total</strong>"
            b"<strong>First 20 results shown, there are 2 in total</strong>"
        )
    with pytest.raises(arun.ArunParseError, match="result table"):
        arun._parse_search_results(
            b"<div>No records are deleted</div>"
            b'<a href="planningDetails?reference=A">A</a>'
        )

    with pytest.raises(arun.ArunParseError, match="result table"):
        arun._parse_search_results(
            b'<a href="planningDetails?reference=BR/1/26/PL">BR/1/26/PL</a>'
        )
    explicit_complete = arun._parse_search_results(
        _complete_results(("PE/PA/6/01/AG", "PE/R/2/02/TEL"))
    )
    assert explicit_complete.reported is None
    assert len(explicit_complete.references) == 2
    assert explicit_complete.references[0].reference == "PE/PA/6/01/AG"
    with pytest.raises(arun.ArunParseError, match="result pagination"):
        arun._parse_search_results(
            b'<strong>1 record</strong><a rel="next" href="?page=2">Next</a>'
            b'<a href="planningDetails?reference=BR/1/26/PL">BR/1/26/PL</a>'
        )


@pytest.mark.parametrize(
    "href",
    [
        "https://elsewhere.invalid/planningDetails?reference=BR/1/26/PL",
        f"{arun.BASE_URL}/other/planningDetails?reference=BR/1/26/PL",
        f"{arun.BASE_URL}/planningDetails?reference=BR/1/26/PL&extra=1",
        f"{arun.BASE_URL}/planningDetails?reference=BR/1/26/PL&extra=",
    ],
)
def test_arun_result_links_stay_on_the_exact_official_route(href: str) -> None:
    with pytest.raises(arun.ArunParseError, match="result reference"):
        arun._parse_search_results(
            _complete_results(("BR/1/26/PL",)).replace(
                b"planningDetails?reference=BR/1/26/PL&amp;from=planningSearch",
                href.replace("&", "&amp;").encode(),
            )
        )


def test_arun_result_membership_is_owned_by_each_validated_table_row() -> None:
    outside = (
        _complete_results(("INSIDE/1",)).replace(
            b'<a href="planningDetails?reference=INSIDE/1&amp;from=planningSearch">'
            b"INSIDE/1</a>",
            b"INSIDE/1",
        )
        + b'<a href="planningDetails?reference=OUTSIDE/1">outside</a>'
    )

    with pytest.raises(arun.ArunParseError, match="outside table"):
        arun._parse_search_results(outside)

    base = _complete_results(("INSIDE/1",))
    with pytest.raises(arun.ArunParseError, match="result row"):
        arun._parse_search_results(base.replace(b"<td>Site</td>", b"", 1))
    with pytest.raises(arun.ArunParseError, match="result reference"):
        arun._parse_search_results(
            base.replace(
                b'<a href="planningDetails?reference=INSIDE/1&amp;from=planningSearch">'
                b"INSIDE/1</a>",
                b'<a href="other">INSIDE/1</a>',
            )
        )
    wrong_cell = base.replace(
        b'<a href="planningDetails?reference=INSIDE/1&amp;from=planningSearch">'
        b"INSIDE/1</a>",
        b"INSIDE/1",
    ).replace(
        b"<td>Site</td>",
        b'<td><a href="planningDetails?reference=INSIDE/1">Site</a></td>',
        1,
    )
    with pytest.raises(arun.ArunParseError, match="result reference"):
        arun._parse_search_results(wrong_cell)
    with pytest.raises(arun.ArunParseError, match="result reference"):
        arun._parse_search_results(
            base.replace(
                b'<a href="planningDetails?reference=INSIDE/1&amp;from=planningSearch">'
                b"INSIDE/1</a>",
                b'<a href="planningDetails?from=planningSearch">INSIDE/1</a>',
            )
        )
    with pytest.raises(arun.ArunParseError, match="result reference label"):
        arun._parse_search_results(base.replace(b">INSIDE/1</a>", b">OTHER/1</a>"))


def test_arun_fetch_rejects_an_untrusted_retained_locator() -> None:
    reference = SourceReference(
        source_id=arun.SOURCE,
        reference="BR/1/26/PL",
        locator=(
            "https://elsewhere.invalid/planningDetails?"
            "reference=BR%2F1%2F26%2FPL&from=planningSearch"
        ),
    )
    session = _Session(_QualificationResponder())

    with pytest.raises(arun.ArunRoutingError):
        asyncio.run(arun.ArunAdapter().fetch(session, reference))

    assert session.requests == []


def test_arun_discovery_resumes_show_all_and_terminal_rerun_has_no_io() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )

    async def first_batch() -> DiscoveryBatch[arun.ArunCheckpointV1]:
        batches = cast(
            "AsyncGenerator[DiscoveryBatch[arun.ArunCheckpointV1]]",
            adapter.discover(_Session(_DiscoveryResponder()), window, None),
        )
        first = await anext(batches)
        await batches.aclose()
        return first

    first = asyncio.run(first_batch())
    assert isinstance(first, DiscoveryBatch)
    assert first.references == ()
    assert len(first.evidence) == 2
    assert isinstance(first.next_checkpoint.cursor, arun.ArunLiveCursor)
    assert isinstance(first.next_checkpoint.cursor.progress, arun.ArunAwaitingShowAll)

    resumed_session = _Session(_DiscoveryResponder())
    resumed = asyncio.run(
        _batches(adapter, resumed_session, window, first.next_checkpoint)
    )
    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == ["BR/1/26/PL", "BR/2/26/PL"]
    terminal = resumed[-1].next_checkpoint
    assert resumed[-1].complete
    assert isinstance(terminal.cursor, arun.ArunLiveCursor)
    assert isinstance(terminal.cursor.progress, arun.ArunComplete)
    assert len(terminal.cursor.progress.completed) == 60
    assert terminal.cursor.progress.seen_references == _DiscoveryResponder.references
    assert terminal.cursor.search_form_evidence is not None
    assert all(
        item.initial_evidence is not None for item in terminal.cursor.progress.completed
    )
    assert terminal.cursor.progress.completed[0].references == (
        "BR/1/26/PL",
        "BR/2/26/PL",
    )

    terminal_session = _Session(_DiscoveryResponder())
    rerun = asyncio.run(_batches(adapter, terminal_session, window, terminal))
    assert len(rerun) == 1
    assert rerun[0].complete
    assert rerun[0].references == ()
    assert terminal_session.requests == []


def test_arun_active_query_resume_adopts_a_new_exact_first_page() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )

    async def first_batch() -> DiscoveryBatch[arun.ArunCheckpointV1]:
        batches = cast(
            "AsyncGenerator[DiscoveryBatch[arun.ArunCheckpointV1]]",
            adapter.discover(_Session(_DiscoveryResponder()), window, None),
        )
        first = await anext(batches)
        await batches.aclose()
        return first

    first = asyncio.run(first_batch())

    def changed(request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        values = {field.name: field.value for field in request.form}
        if values.get("showall") == "showall":
            return _complete_results(("NEW/1", "BR/1/26/PL", "BR/2/26/PL"))
        if values.get("receivedFrom") == "18-08-26":
            fields = "".join(
                f'<input type="hidden" name="{name}" value="{value}">'
                for name, value in values.items()
                if name != "action"
            )
            return (
                _partial_results(fields)
                .replace(
                    b"there are 2 in total",
                    b"there are 3 in total",
                )
                .replace(b"BR/1/26/PL", b"NEW/1")
            )
        return _empty_results()

    resumed = asyncio.run(
        _batches(adapter, _Session(changed), window, first.next_checkpoint)
    )

    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == ["NEW/1", "BR/1/26/PL", "BR/2/26/PL"]
    terminal = resumed[-1].next_checkpoint.cursor
    assert isinstance(terminal, arun.ArunLiveCursor)
    assert isinstance(terminal.progress, arun.ArunComplete)
    assert terminal.search_form_evidence is not None
    assert terminal.progress.completed[0].reported_count == 3
    assert terminal.progress.completed[0].references == (
        "NEW/1",
        "BR/1/26/PL",
        "BR/2/26/PL",
    )

    def changed_to_exact(request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        values = {field.name: field.value for field in request.form}
        if values.get("receivedFrom") == "18-08-26":
            return _complete_results(("NEW/1",))
        return _empty_results()

    exact = asyncio.run(
        _batches(adapter, _Session(changed_to_exact), window, first.next_checkpoint)
    )
    assert [
        reference.reference for batch in exact for reference in batch.references
    ] == ["NEW/1"]
    assert exact[-1].complete
    exact_terminal = exact[-1].next_checkpoint.cursor
    assert isinstance(exact_terminal, arun.ArunLiveCursor)
    assert isinstance(exact_terminal.progress, arun.ArunComplete)
    assert exact_terminal.search_form_evidence is not None
    assert exact_terminal.progress.seen_references == ("NEW/1",)
    assert exact_terminal.progress.completed[0].references == ("NEW/1",)

    def changed_exact_with_show_all(request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        values = {field.name: field.value for field in request.form}
        fields = "".join(
            f'<input type="hidden" name="{name}" value="{value}">'
            for name, value in values.items()
            if name != "action"
        )
        return _partial_results(fields).replace(b"there are 2", b"there are 1")

    with pytest.raises(arun.ArunParseError, match="partial result count"):
        asyncio.run(
            _batches(
                adapter,
                _Session(changed_exact_with_show_all),
                window,
                first.next_checkpoint,
            )
        )

    def changed_without_show_all(request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        return (
            b"<strong>First 1 results shown, there are 3 in total</strong>"
            + _complete_results(("NEW/1",))
        )

    with pytest.raises(arun.ArunParseError, match="partial result count"):
        asyncio.run(
            _batches(
                adapter,
                _Session(changed_without_show_all),
                window,
                first.next_checkpoint,
            )
        )


def test_arun_live_checkpoint_rejects_a_different_scope() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    scope = arun.ArunDiscoveryScope.model_validate(window.model_dump())
    plan = arun._canonical_query_plan(scope)
    checkpoint = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunReady(next_query=0),
        )
    )
    changed = window.model_copy(update={"start": date(2026, 8, 19)})

    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(
            _batches(adapter, _Session(_DiscoveryResponder()), changed, checkpoint)
        )


def test_arun_completed_checkpoint_starts_a_new_scope() -> None:
    adapter = arun.ArunAdapter()
    first_window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    first_batches = asyncio.run(
        _batches(adapter, _Session(_DiscoveryResponder()), first_window, None)
    )
    terminal = first_batches[-1].next_checkpoint
    second_window = first_window.model_copy(update={"start": date(2026, 8, 19)})
    second_session = _Session(_DiscoveryResponder())

    second_batches = asyncio.run(
        _batches(adapter, second_session, second_window, terminal)
    )

    assert second_batches[-1].complete
    second_cursor = second_batches[-1].next_checkpoint.cursor
    assert isinstance(second_cursor, arun.ArunLiveCursor)
    assert second_cursor.scope.start == second_window.start
    assert second_session.requests


def test_arun_reference_identity_is_unique_across_overlapping_queries() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )
    session = _Session(_DiscoveryResponder())

    batches = asyncio.run(_batches(adapter, session, window, None))

    references = tuple(
        reference.reference for batch in batches for reference in batch.references
    )
    assert references == _DiscoveryResponder.references
    assert all(
        reference.source_id == arun.SOURCE
        for batch in batches
        for reference in batch.references
    )
    assert not any(
        isinstance(reference, SourceReference) and reference.reference == ""
        for batch in batches
        for reference in batch.references
    )


def test_arun_checkpoint_invariants_reject_contradictory_progress() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    plan = arun._canonical_query_plan(scope)
    summary = arun.ArunCompletedQuery(
        key=plan[0].key,
        reported_count=0,
        enumerated_count=0,
        references=(),
        initial_evidence=_SEARCH_DIGEST,
    )

    with pytest.raises(ValueError, match="counts disagree"):
        arun.ArunCompletedQuery(
            key=plan[0].key,
            reported_count=1,
            enumerated_count=0,
            references=("A",),
            initial_evidence=_SEARCH_DIGEST,
        )
    with pytest.raises(ValueError, match="not a plan prefix"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunReady(
                next_query=1,
                completed=(summary.model_copy(update={"key": "wrong"}),),
            ),
        )
    with pytest.raises(ValueError, match="not unique"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunReady(
                next_query=0,
                seen_references=("A", "A"),
            ),
        )
    with pytest.raises(ValueError, match="does not cover"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunComplete(completed=()),
        )
    with pytest.raises(ValueError, match="does not follow"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunReady(next_query=1),
        )
    with pytest.raises(ValueError, match="not unique"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunAwaitingShowAll(
                next_query=0,
                reported_count=2,
                initial_references=("A", "A"),
                initial_evidence=_SEARCH_DIGEST,
            ),
        )


def test_arun_checkpoint_modes_cannot_cross_transport_boundaries() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    scope = arun.ArunDiscoveryScope.model_validate(window.model_dump())
    live = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=scope,
            plan=arun._canonical_query_plan(scope),
            progress=arun.ArunReady(next_query=0),
        )
    )
    fixture = arun.ArunCheckpointV1(cursor=arun.ArunFixtureCursor(result_row="first"))

    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(
            _batches(
                adapter,
                _Session(_DiscoveryResponder(), mode=TransportMode.FIXTURE),
                window,
                live,
            )
        )
    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(_batches(adapter, _Session(_DiscoveryResponder()), window, fixture))


def test_arun_legacy_v1_checkpoints_decode_and_restart_safely() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    legacy_live = arun.ArunCheckpointV1.model_validate_json(
        json.dumps(
            {
                "result_row": "live",
                "live_phase": "show-all",
                "window_start": "2026-08-18",
                "window_end": "2026-09-16",
                "seen_references": ["BR/1/26/PL"],
            }
        )
    )

    batches = asyncio.run(
        _batches(adapter, _Session(_DiscoveryResponder()), window, legacy_live)
    )

    assert batches[-1].complete
    assert isinstance(batches[-1].next_checkpoint.cursor, arun.ArunLiveCursor)
    assert isinstance(
        batches[-1].next_checkpoint.cursor.progress,
        arun.ArunComplete,
    )
    legacy_fixture = arun.ArunCheckpointV1.model_validate_json(
        '{"result_row":"second"}'
    )
    assert legacy_fixture.cursor == arun.ArunFixtureCursor(result_row="second")
    mismatched = arun.ArunCheckpointV1.model_validate_json(
        '{"result_row":"live","window_start":"2026-08-17","window_end":"2026-09-16"}'
    )
    with pytest.raises(arun.ArunCheckpointError):
        asyncio.run(
            _batches(adapter, _Session(_DiscoveryResponder()), window, mismatched)
        )


def test_arun_form_and_show_all_structure_fail_closed() -> None:
    query = arun.ArunReceivedQuery(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
    )
    with pytest.raises(arun.ArunParseError, match="form method"):
        arun._parse_search_form(
            _search_form().replace(b'method="post"', b'method="get"')
        )
    with pytest.raises(arun.ArunParseError, match="search controls"):
        arun._parse_search_form(
            _search_form().replace(b'name="receivedTo"', b'name="other"')
        )
    with pytest.raises(arun.ArunParseError, match="search submit"):
        arun._parse_search_form(
            _search_form().replace(b'value="Search"', b'value="Find"')
        )

    values = arun._query_values(query)
    fields = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in values.items()
    )
    results = arun._parse_search_results(_partial_results(fields))
    show_all = results.show_all_form
    assert show_all is not None
    duplicate = show_all.model_copy(
        update={"fields": (*show_all.fields, show_all.fields[0])}
    )
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(duplicate, query)
    wrong_action = show_all.model_copy(
        update={
            "fields": tuple(
                FormField(name=field.name, value="Reset")
                if field.name == "action"
                else field
                for field in show_all.fields
            )
        }
    )
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(wrong_action, query)
    with pytest.raises(arun.ArunResultCapError):
        arun._parse_search_results(
            b"<strong>First 20 results shown, there are 200 in total</strong>"
        )
    with pytest.raises(arun.ArunParseError, match="partial result count"):
        arun._parse_search_results(
            _partial_results(fields).replace(b"First 1", b"First 2")
        )
    with pytest.raises(arun.ArunParseError, match="partial result count"):
        arun._parse_search_results(
            _partial_results(fields).replace(b"there are 2", b"there are 1")
        )
    with pytest.raises(arun.ArunParseError, match="partial result count"):
        arun._parse_search_results(
            _partial_results(fields).replace(b'name="showall"', b'name="other"')
        )
    with pytest.raises(arun.ArunParseError, match="reported result count"):
        arun._parse_search_results(
            _partial_results(fields).replace(
                b"<strong>First 1 results shown, there are 2 in total</strong>",
                b"",
            )
        )
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(None, query)
    with pytest.raises(arun.ArunParseError):
        arun._parse_search_results(
            _partial_results(fields)
            + _partial_results(fields).replace(b"BR/1/26/PL", b"BR/2/26/PL")
        )
    with pytest.raises(arun.ArunParseError, match="show all form method"):
        arun._parse_search_results(
            _partial_results(fields).replace(b'method="post"', b'method="get"')
        )
    with pytest.raises(arun.ArunParseError, match="show all form"):
        arun._parse_search_results(
            _partial_results(fields)
            + b'<form method="post"><input name="showall" value="showall"></form>'
        )


def test_arun_active_query_replay_and_ambiguous_exact_results_fail_closed() -> None:
    adapter = arun.ArunAdapter()
    window = DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )
    scope = arun.ArunDiscoveryScope.model_validate(window.model_dump())
    plan = arun._canonical_query_plan(scope)

    def ambiguous(request: PortalRequest) -> bytes:
        if request.method == RequestMethod.GET:
            return _search_form()
        values = {field.name: field.value for field in request.form}
        fields = "".join(
            f'<input type="hidden" name="{name}" value="{value}">'
            for name, value in values.items()
            if name != "action"
        )
        return _partial_results(fields).replace(b"there are 2", b"there are 1")

    with pytest.raises(arun.ArunParseError, match="partial result count"):
        asyncio.run(_batches(adapter, _Session(ambiguous), window, None))

    terminal = arun.ArunLiveCursor(
        scope=scope,
        plan=plan,
        progress=arun.ArunComplete(
            completed=tuple(
                arun.ArunCompletedQuery(
                    key=query.key,
                    reported_count=0,
                    enumerated_count=0,
                    references=(),
                    initial_evidence=_SEARCH_DIGEST,
                )
                for query in plan
            )
        ),
    )
    with pytest.raises(arun.ArunCheckpointError):
        arun._complete_query(
            terminal,
            arun.ArunCompletedQuery(
                key=plan[0].key,
                reported_count=0,
                enumerated_count=0,
                references=(),
                initial_evidence=_SEARCH_DIGEST,
            ),
        )


def test_arun_fetch_retains_rich_document_metadata_without_attachment_bodies() -> None:
    reference = SourceReference(
        source_id=arun.SOURCE,
        reference="BR/1/26/PL",
        locator=f"{arun.BASE_URL}/planningDetails?reference=BR%2F1%2F26%2FPL",
    )

    def responder(request: PortalRequest) -> bytes:
        url = str(request.url)
        if "planningDetails" in url:
            return _detail_with_documents(reference.reference)
        if "showDocuments" in url:
            assert request.method == RequestMethod.POST
            assert [(field.name, field.value) for field in request.form] == [
                ("ViewDocuments", "View Documents")
            ]
            return _document_index()
        raise AssertionError(url)

    session = _Session(responder)
    snapshot = asyncio.run(arun.ArunAdapter().fetch(session, reference))

    assert snapshot.completeness.documents.kind == "complete"
    assert snapshot.completeness.comments.kind == "unavailable"
    assert len(snapshot.evidence) == 2
    assert len(snapshot.payload.documents) == 2
    decision, plan = snapshot.payload.documents
    assert isinstance(decision, arun.ArunDocumentV1)
    assert decision.title == "Decision notice"
    assert decision.document_type == "Decision"
    assert decision.published_date == date(2026, 9, 15)
    assert decision.description == "Decision notice"
    assert len(decision.source_links) == 1
    assert plan.title == "Plan"
    assert plan.description is None
    assert all("viewDocument" not in url for url in session.requested_urls)
    assert snapshot.payload.received_date == date(2026, 9, 1)
    assert snapshot.payload.validated_date == date(2026, 9, 2)
    assert snapshot.payload.decision_by_date == date(2026, 11, 1)
    assert snapshot.payload.comment_by_date == date(2026, 9, 30)
    assert snapshot.payload.target_committee_date == date(2026, 10, 15)

    decided = snapshot.model_copy(
        update={
            "payload": snapshot.payload.model_copy(
                update={
                    "decision_status": "Application Permitted",
                    "decision_date": date(2026, 9, 15),
                }
            )
        }
    )
    normalised = arun.ArunAdapter().normalise(decided)
    assert normalised.metadata.decision == "Application Permitted"
    assert [event.event_type for event in normalised.metadata.events] == [
        "decision-due",
        "comment-deadline",
        "target-committee",
    ]
    on_hold = arun.ArunAdapter().normalise(
        snapshot.model_copy(
            update={
                "payload": snapshot.payload.model_copy(
                    update={"decision_status": "Undecided\u00a0\u00a0\u00a0(On Hold)"}
                )
            }
        )
    )
    assert on_hold.status == "undecided-(on-hold)"
    assert on_hold.normaliser_version == "arun-v5"

    without_locator = asyncio.run(
        arun.ArunAdapter().fetch(
            _Session(responder),
            reference.model_copy(update={"locator": None}),
        )
    )
    assert without_locator.payload.ocella_reference == reference.reference

    retained = RetainedNativeRecord(
        application_id=ApplicationId("arun-reference-proof"),
        authority_id=AuthorityId("arun"),
        reference=reference,
        native_schema="ArunApplicationV1",
        native_json=snapshot.payload.model_dump_json(),
        observed_at=snapshot.observed_at,
        completeness=snapshot.completeness,
        evidence=snapshot.evidence,
    )
    agrees = _qualification_module()._native_evidence_agrees
    assert agrees(retained, snapshot.payload)
    assert not agrees(
        retained.model_copy(
            update={"reference": reference.model_copy(update={"reference": "OTHER/1"})}
        ),
        snapshot.payload,
    )
    assert not agrees(
        retained,
        snapshot.payload.model_copy(update={"ocella_reference": "OTHER/1"}),
    )
    assert not agrees(
        retained,
        snapshot.payload.model_copy(update={"proposal_text": "Different proposal"}),
    )


def test_arun_appeal_block_is_preserved_without_becoming_application_type() -> None:
    reference = SourceReference(
        source_id=arun.SOURCE,
        reference="R/271/06/TEL",
        locator=f"{arun.BASE_URL}/planningDetails?reference=R%2F271%2F06%2FTEL",
    )
    appeal_rows = b"""
      <tr><th>Appeal</th><td>
        <a href="appealDetails?appeal=1671&amp;back=no">1671</a>
      </td></tr>
      <tr><th>Lodged</th><td>19-12-06</td></tr>
      <tr><th>Type</th><td>Dismissed</td></tr>
      <tr><th>Decision</th><td>03-05-07</td></tr>
    """
    detail = _detail_with_documents(reference.reference).replace(
        b"</table>", appeal_rows + b"</table>", 1
    )

    def responder(request: PortalRequest) -> bytes:
        return detail if "planningDetails" in str(request.url) else _document_index()

    snapshot = asyncio.run(arun.ArunAdapter().fetch(_Session(responder), reference))
    normalised = arun.ArunAdapter().normalise(snapshot)

    assert snapshot.payload.application_type is None
    assert snapshot.payload.appeal_reference == "1671"
    assert snapshot.payload.appeal_status == "Dismissed"
    assert snapshot.payload.appeal_lodged_date == date(2006, 12, 19)
    assert snapshot.payload.appeal_decision_date == date(2007, 5, 3)
    assert [event.event_type for event in normalised.metadata.events][-2:] == [
        "appeal-lodged",
        "appeal-decision",
    ]
    assert normalised.metadata.events[-1].details == "Dismissed"
    assert normalised.metadata.relationships[0].model_dump() == {
        "related_reference": "1671",
        "relationship_type": "appeal",
    }


def test_arun_v1_native_documents_remain_offline_rebuildable() -> None:
    reference = SourceReference(source_id=arun.SOURCE, reference="LEGACY/1")
    body = json.dumps(
        {
            "ocella_reference": "LEGACY/1",
            "proposal_text": "Legacy proposal",
            "decision_status": "Undecided",
            "parish_name": "Arun",
            "documents": [
                {
                    "title": "Legacy plan",
                    "url": "https://www1.arun.gov.uk/legacy-plan.pdf",
                }
            ],
        }
    )
    capture = EvidenceCapture(
        url=HttpUrl(f"{arun.BASE_URL}/planningDetails?reference=LEGACY%2F1"),
        media_type="text/html",
        body=b"legacy",
        digest=EvidenceDigest(sha256(b"legacy").hexdigest()),
    )
    retained = RetainedNativeRecord(
        application_id=ApplicationId("legacy-id"),
        authority_id=AuthorityId("arun"),
        reference=reference,
        native_schema="ArunApplicationV1",
        native_json=body,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=CompleteSection(item_count=1),
            comments=UnavailableSection(reason="legacy"),
        ),
        evidence=(capture,),
    )

    rebuilt = ARUN_PACKAGE.rebuild(retained)

    assert rebuilt.documents[0].title == "Legacy plan"
    assert rebuilt.normaliser_version == "arun-v5"
    assert not _qualification_module()._native_evidence_agrees(
        retained,
        arun.ArunApplicationV1.model_validate_json(body),
    )


def test_arun_reference_unions_drop_duplicates_in_stable_order() -> None:
    one = SourceReference(source_id=arun.SOURCE, reference="ONE")
    two = SourceReference(source_id=arun.SOURCE, reference="TWO")
    assert arun._fresh((one, two), ("ONE",)) == ((two,), ("ONE", "TWO"))
    completed = (
        arun.ArunCompletedQuery(
            key="one",
            reported_count=2,
            enumerated_count=2,
            references=("ONE", "TWO"),
            initial_evidence=_SEARCH_DIGEST,
        ),
        arun.ArunCompletedQuery(
            key="two",
            reported_count=1,
            enumerated_count=1,
            references=("ONE",),
            initial_evidence=_SEARCH_DIGEST,
        ),
    )
    assert arun._completed_references(completed) == ("ONE", "TWO")


def test_arun_fetch_accepts_an_application_without_a_parish_label() -> None:
    reference = SourceReference(
        source_id=arun.SOURCE,
        reference="BR/1/26/PL",
        locator=f"{arun.BASE_URL}/planningDetails?reference=BR%2F1%2F26%2FPL",
    )

    def responder(request: PortalRequest) -> bytes:
        if "planningDetails" in str(request.url):
            return _detail_with_documents(reference.reference).replace(
                b"<tr><th>Parish</th><td>Bognor Regis</td></tr>",
                b"",
            )
        return _empty_document_index()

    snapshot = asyncio.run(arun.ArunAdapter().fetch(_Session(responder), reference))

    assert snapshot.payload.parish_name is None
    assert snapshot.completeness.documents.kind == "empty"


def test_arun_document_action_and_index_fail_closed_on_ambiguous_shapes() -> None:
    detail = _detail_with_documents("BR/1/26/PL")
    with pytest.raises(arun.ArunParseError, match="document action reference"):
        arun._document_request(detail, "OTHER/1")
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(detail + detail, "BR/1/26/PL")
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(
            detail.replace(
                b"showDocuments?reference=BR/1/26/PL&amp;module=pl",
                b"other/showDocuments?reference=BR/1/26/PL&amp;module=pl",
            ),
            "BR/1/26/PL",
        )
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(
            detail.replace(b"&amp;module=pl", b"&amp;module=pl&amp;extra=1"),
            "BR/1/26/PL",
        )
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(
            detail.replace(b"&amp;module=pl", b"&amp;module=pl&amp;extra="),
            "BR/1/26/PL",
        )
    with pytest.raises(arun.ArunParseError, match="document pagination"):
        arun._parse_document_index(
            _document_index() + b'<nav class="pagination">next</nav>'
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(b'value="" selected', b'value="PLAN" selected')
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(
                b'<option value="" selected>All</option>',
                b'<option value="PLAN">Plan</option>',
            )
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(
                b'<option value="" selected>All</option>',
                b"",
            )
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(
                b'<option value="" selected>All</option>',
                b'<option value="" selected>All</option>'
                b'<option value="PLAN" selected>Plan</option>',
            )
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(_document_filter(), b"", 1)
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(
                _document_filter(),
                _document_filter() + _document_filter(),
                1,
            )
        )
    with pytest.raises(arun.ArunParseError, match="document table"):
        arun._parse_document_index(
            _document_filter() + b"No documents found in unrelated help text"
        )


def test_arun_document_index_accepts_the_official_headerless_table() -> None:
    documents = arun._parse_document_index(_headerless_document_index())

    assert len(documents) == 1
    assert documents[0].document_type == "Application"
    assert documents[0].published_date == date(2026, 9, 15)
    assert documents[0].description == "Application Form - Without Personal Data"


def test_arun_document_index_accepts_the_official_empty_section() -> None:
    documents = arun._parse_document_index(_empty_document_index())

    assert documents == ()


def test_arun_document_index_rejects_ambiguous_empty_and_link_shapes() -> None:
    with pytest.raises(arun.ArunParseError, match="document rows"):
        arun._parse_document_index(
            _document_filter() + b"<strong>Documents</strong><table>"
            b"<tr><th>Type</th><th>Date</th></tr></table>"
        )
    official_empty = _empty_document_index()
    with pytest.raises(arun.ArunParseError, match="document empty state"):
        arun._parse_document_index(
            official_empty + _document_index().replace(_document_filter(), b"", 1)
        )
    ambiguous = _document_index().replace(
        b"Decision</a>",
        (
            b'Decision</a><a href="https://elsewhere.invalid/'
            b'viewDocument?file=other.pdf&amp;module=pl">other</a>'
        ),
        1,
    )
    with pytest.raises(arun.ArunParseError, match="document link"):
        arun._parse_document_index(ambiguous)
    unrelated = _document_index().replace(
        b"viewDocument?file=decision-1.pdf&amp;module=pl",
        b"other?file=decision-1.pdf&amp;module=pl",
        1,
    )
    with pytest.raises(arun.ArunParseError, match="document link"):
        arun._parse_document_index(unrelated)
    with pytest.raises(arun.ArunParseError, match="outside table"):
        arun._parse_document_index(
            _document_index()
            + b'<a href="viewDocument?file=outside.pdf&amp;module=pl">outside</a>'
        )


def test_arun_appeal_block_rejects_ambiguous_or_incomplete_shapes() -> None:
    block = b"""
      <table>
        <tr><th>Appeal</th><td>123</td></tr>
        <tr><th>Lodged</th><td>19-12-06</td></tr>
        <tr><th>Type</th><td>Dismissed</td></tr>
        <tr><th>Decision</th><td>03-05-07</td></tr>
      </table>
    """
    with pytest.raises(arun.ArunParseError, match="appeal block"):
        arun._parse_appeal_fields(block + block)
    with pytest.raises(arun.ArunParseError, match="appeal block"):
        arun._parse_appeal_fields(
            block.replace(
                b"<tr><th>Lodged</th><td>19-12-06</td></tr>",
                b"<tr><td>orphan</td></tr>",
            )
        )
    with pytest.raises(arun.ArunParseError, match="appeal block"):
        arun._parse_appeal_fields(block.replace(b"<th>Type</th>", b"<th>State</th>"))
    with pytest.raises(arun.ArunParseError, match="appeal block"):
        arun._parse_appeal_fields(
            block.replace(
                b"</table>",
                b"<tr><th>Later</th><td>silently discarded</td></tr></table>",
            )
        )


def test_arun_detail_fields_reject_duplicate_normalised_labels() -> None:
    detail = _detail_with_documents("BR/1/26/PL").replace(
        b"<tr><th>Status</th><td>Undecided</td></tr>",
        b"<tr><th>Status</th><td>Undecided</td></tr>"
        b"<tr><th> status </th><td>Different</td></tr>",
    )

    with pytest.raises(arun.ArunParseError, match="duplicate labelled detail field"):
        arun._parse_labelled_fields(detail)


@pytest.mark.parametrize(
    "replacement",
    [
        b'<form method="get" action="showDocuments',
        b'<form method="post" action="https://elsewhere.invalid/showDocuments',
        b'<form method="post" action="showDocuments?module=wrong&amp;ignored=',
    ],
)
def test_arun_document_action_rejects_wrong_routing(replacement: bytes) -> None:
    detail = _detail_with_documents("BR/1/26/PL")
    malformed = detail.replace(
        b'<form method="post" action="showDocuments',
        replacement,
    )
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(malformed, "BR/1/26/PL")


def test_arun_document_action_requires_the_exact_submit_control() -> None:
    detail = _detail_with_documents("BR/1/26/PL").replace(
        b'name="ViewDocuments"', b'name="Other"'
    )
    with pytest.raises(arun.ArunParseError, match="document action submit"):
        arun._document_request(detail, "BR/1/26/PL")


@pytest.mark.parametrize(
    "href",
    [
        "https://elsewhere.invalid/viewDocument?file=x.pdf&module=pl",
        "notviewDocument?file=x.pdf&module=pl",
        "viewDocument?module=pl",
        "viewDocument?file=x.pdf&module=wrong",
        "http://www1.arun.gov.uk/aplanning/OcellaWeb/viewDocument?file=x.pdf&module=pl",
        "https://www1.arun.gov.uk/other/viewDocument?file=x.pdf&module=pl",
        "viewDocument?file=x.pdf&module=pl&extra=",
        "viewDocument?file=x.pdf&module=pl#fragment",
    ],
)
def test_arun_document_index_rejects_invalid_attachment_links(href: str) -> None:
    malformed = _document_index().replace(
        b"viewDocument?file=decision-1.pdf&amp;module=pl",
        href.replace("&", "&amp;").encode(),
        1,
    )
    with pytest.raises(arun.ArunParseError, match="document link"):
        arun._parse_document_index(malformed)


def test_arun_document_index_rejects_incomplete_tables_and_dates() -> None:
    with pytest.raises(arun.ArunParseError, match="document table"):
        arun._parse_document_index(_document_filter() + b"unknown document response")
    with pytest.raises(arun.ArunParseError, match="document table"):
        arun._parse_document_index(
            _document_index() + _document_index().replace(_document_filter(), b"", 1)
        )
    with pytest.raises(arun.ArunParseError, match="document row"):
        arun._parse_document_index(
            _document_filter() + b"<table><tr><th>Type</th><th>Date</th></tr>"
            b"<tr><td>Only</td></tr></table>"
        )
    with pytest.raises(arun.ArunParseError, match="document row"):
        arun._parse_document_index(
            _document_index().replace(
                b"<td>Decision notice</td>",
                b"<td>Decision notice</td><td>Unexpected</td>",
                1,
            )
        )
    with pytest.raises(arun.ArunParseError, match="document link"):
        arun._parse_document_index(
            _document_filter() + b"<table><tr><th>Type</th><th>Date</th></tr>"
            b"<tr><td>Type</td><td></td><td></td><td></td><td></td></tr></table>"
        )
    blank_date = arun._parse_document_index(
        _document_index().replace(b"15/09/2026", b"", 1)
    )
    assert blank_date[0].published_date is None
    with pytest.raises(arun.ArunParseError, match="document date"):
        arun._parse_document_index(
            _document_index().replace(b"15/09/2026", b"not-a-date", 1)
        )


def test_arun_qualification_requires_explicit_safe_options(
    tmp_path: "Path",
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

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("preserve", encoding="utf-8")
    args = [
        "--confirm-live",
        "--data-dir",
        str(occupied),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(args, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert created == 0


def test_arun_qualification_reports_a_stable_parse_boundary(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    args = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "bad-source"),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]

    result = module.main(
        args,
        session_factory=lambda: _Session(lambda _request: b"<html></html>"),
    )

    assert result == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "runtime-failure",
        "exception": "ArunParseError",
        "source_error": "parse-planning-search-form",
    }


def test_arun_qualification_does_not_hide_programmer_defects(tmp_path: "Path") -> None:
    module = _qualification_module()
    args = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "programmer-defect"),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]

    with pytest.raises(AssertionError, match="programmer defect"):
        module.main(
            args,
            session_factory=lambda: _Session(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("programmer defect")
                )
            ),
        )


def test_arun_qualification_receipt_proves_exact_state_and_zero_network_rerun(  # noqa: PLR0915
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
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
    _assert_sessions_closed_and_rerun_empty(sessions)
    assert len(sessions[0].requested_urls) == 66
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 3
    assert receipt["authority_id"] == "arun"
    assert receipt["bootstrap_status"] == "proved"
    assert receipt["operational_status"] == "pending-weekly-refreshes"
    assert receipt["registry_readiness"] == "discovery-only"
    assert len(receipt["query_inventory"]) == 60
    assert receipt["query_inventory"][0]["query"] == {
        "kind": "received",
        "start": "2026-08-18",
        "end": "2026-09-16",
    }
    assert len(receipt["query_inventory"][0]["initial_evidence_digest"]) == 64
    assert len(receipt["query_inventory"][0]["expanded_evidence_digest"]) == 64
    _assert_first_receipt_request_is_bound(receipt)
    assert receipt["query_inventory"][0]["source_reported_count"] == 2
    assert receipt["query_inventory"][0]["enumerated_count"] == 2
    assert receipt["query_inventory"][0]["references"] == [
        "BR/1/26/PL",
        "BR/2/26/PL",
    ]
    assert receipt["query_inventory"][2]["source_reported_count"] == 0
    assert receipt["query_inventory"][2]["references"] == []
    assert receipt["query_inventory"][-1]["query"]["end"] == "2026-09-16"
    assert receipt["references"] == {
        "discovered": ["BR/1/26/PL", "BR/2/26/PL"],
        "retained_native": ["BR/1/26/PL", "BR/2/26/PL"],
        "applications": ["BR/1/26/PL", "BR/2/26/PL"],
    }
    assert receipt["native_coverage"] == {
        "applications": 2,
        "appeal_references": 0,
        "appeal_statuses": 0,
        "appeal_lodged_dates": 0,
        "appeal_decision_dates": 0,
    }
    prior_v3 = dict(receipt)
    prior_v3.pop("native_coverage")
    prior_v3["query_inventory"] = [
        {
            key: value
            for key, value in query.items()
            if key not in {"initial_request", "expanded_request"}
        }
        for query in receipt["query_inventory"]
    ]
    assert (
        module.ArunQualificationReceiptV3.model_validate(prior_v3).native_coverage
        is None
    )
    assert receipt["evidence"]["application_capture_count"] == 4
    assert len(receipt["evidence"]["application_digests"]) == 4
    assert receipt["evidence"]["search_capture_count"] == 62
    assert len(receipt["evidence"]["search_digests"]) == 62
    assert len(receipt["search_form_evidence_digest"]) == 64
    assert len(receipt["semantic_fingerprint"]) == 64
    assert receipt["costs"]["bootstrap_total"] == {
        "request_count": 66,
        "transferred_bytes": sessions[0].transferred_bytes,
        "attachment_body_requests": 0,
    }
    assert receipt["costs"]["final_resume_attempt"] == {
        "request_count": 66,
        "transferred_bytes": sessions[0].transferred_bytes,
        "attachment_body_requests": 0,
    }
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["weekly_cycles"] == [
        {"target_date": "2026-09-23", "status": "pending"},
        {"target_date": "2026-09-30", "status": "pending"},
    ]
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    provenance = receipt["provenance"]
    assert len(provenance["code_revision"]) == 40
    assert (
        provenance["publication"]["run_id"]
        != provenance["immediate_follow_up"]["run_id"]
    )
    assert provenance["publication"]["cost"] == receipt["costs"]["final_resume_attempt"]
    assert provenance["immediate_follow_up"]["cost"] == receipt["costs"]["rerun"]
    assert (
        provenance["publication"]["started_at"]
        <= provenance["publication"]["finished_at"]
    )
    assert (
        provenance["immediate_follow_up"]["started_at"]
        <= provenance["immediate_follow_up"]["finished_at"]
    )
    assert all(check["ok"] for check in receipt["checks"])
    receipt_path = data_dir / "arun-qualification-v3.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert not (data_dir / ".arun-qualification-v3.json.tmp").exists()


def test_arun_qualification_accepts_a_new_scope_over_cumulative_sqlite_state(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "two-scopes"
    first_args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert (
        module.main(
            first_args,
            session_factory=lambda: _Session(_QualificationResponder()),
        )
        == 0
    )
    first_receipt = json.loads(capsys.readouterr().out)
    old_search_digest = next(
        query["expanded_evidence_digest"] or query["initial_evidence_digest"]
        for query in first_receipt["query_inventory"]
        if any(
            reference["reference"] == "BR/1/26/PL"
            for reference in query["source_references"]
        )
    )

    second_responder = _QualificationResponder()
    second_responder.references = ("BR/3/26/PL", "BR/4/26/PL")
    second_sessions: list[_Session] = []

    def second_factory() -> _Session:
        session = _Session(second_responder)
        second_sessions.append(session)
        return session

    second_args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-25",
        "--end",
        "2026-09-23",
        "--include-open",
        "--resume",
    ]
    assert module.main(second_args, session_factory=second_factory) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert len(second_sessions) == 2
    assert second_sessions[0].requested_urls
    assert second_sessions[1].requested_urls == ()
    assert receipt["scope"] == {
        "start": "2026-08-25",
        "end": "2026-09-23",
        "include_open": True,
    }
    assert receipt["references"] == {
        "discovered": ["BR/1/26/PL", "BR/2/26/PL", "BR/3/26/PL"],
        "retained_native": ["BR/1/26/PL", "BR/2/26/PL", "BR/3/26/PL"],
        "applications": ["BR/1/26/PL", "BR/2/26/PL", "BR/3/26/PL"],
    }
    assert {
        reference
        for query in receipt["query_inventory"]
        for reference in query["references"]
    } == {"BR/3/26/PL"}
    assert receipt["counts"]["applications"] == 3
    assert all(check["ok"] for check in receipt["checks"])

    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        evidence_path = connection.execute(
            "SELECT path FROM evidence WHERE digest = ?",
            (old_search_digest,),
        ).fetchone()[0]
        connection.executescript(
            """
            UPDATE applications
            SET source_id = 'tampered-source',
                locator = 'https://example.test/tampered'
            WHERE reference = 'BR/1/26/PL';
            UPDATE discovery_queue
            SET source_id = 'tampered-source',
                locator = 'https://example.test/tampered'
            WHERE reference = 'BR/1/26/PL';
            UPDATE native_rebuild_inputs
            SET source_id = 'tampered-source',
                locator = 'https://example.test/tampered'
            WHERE reference = 'BR/1/26/PL';
            """
        )
        connection.commit()
    (data_dir / "evidence" / evidence_path).write_bytes(
        gzip.compress(b"<html><body>corrupted historical search</body></html>")
    )

    assert module.main(second_args, session_factory=second_factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "source-evidence-identity" in error["failed_checks"]
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_rejects_checkpoint_source_count_tampering(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "count-tampering"
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
    factory = lambda: _Session(_QualificationResponder())  # noqa: E731

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM checkpoints WHERE authority_id = 'arun'"
            ).fetchone()[0]
        )
        assert payload["cursor"]["progress"]["completed"][4]["reported_count"] is None
        payload["cursor"]["progress"]["completed"][4]["reported_count"] = 1
        connection.execute(
            "UPDATE checkpoints SET payload_json = ? WHERE authority_id = 'arun'",
            (json.dumps(payload, separators=(",", ":")),),
        )
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed", error
    assert "terminal-checkpoint" in error["failed_checks"]
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_fails_cleanly_on_missing_application_captures(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "missing-application-captures"
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
    factory = lambda: _Session(_QualificationResponder())  # noqa: E731

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute(
            """
            UPDATE native_rebuild_inputs
            SET evidence_digests_json = '[]'
            WHERE application_id = (
                SELECT application_id
                FROM native_rebuild_inputs
                ORDER BY application_id
                LIMIT 1
            )
            """
        )
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "native-evidence-agreement" in error["failed_checks"]
    assert "normalised-evidence-agreement" in error["failed_checks"]
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_fails_cleanly_on_a_missing_evidence_row(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "missing-evidence-row"
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
    factory = lambda: _Session(_QualificationResponder())  # noqa: E731

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute(
            """
            DELETE FROM evidence
            WHERE digest = (
                SELECT json_extract(evidence_digests_json, '$[0]')
                FROM native_rebuild_inputs
                ORDER BY application_id
                LIMIT 1
            )
            """
        )
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error": "qualification-failed",
        "failed_checks": ["application-evidence-digests"],
    }
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_rejects_checkpoint_request_tampering(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "request-tampering"
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

    def factory() -> _Session:
        return _Session(_QualificationResponder())

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM checkpoints WHERE authority_id = 'arun'"
            ).fetchone()[0]
        )
        first = payload["cursor"]["progress"]["completed"][0]
        first["initial_request"]["form"][8]["value"] = "19-08-26"
        connection.execute(
            "UPDATE checkpoints SET payload_json = ? WHERE authority_id = 'arun'",
            (json.dumps(payload, separators=(",", ":")),),
        )
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "terminal-checkpoint" in error["failed_checks"]
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_accepts_legacy_terminal_request_contracts(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "legacy-request-contracts"
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
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(_QualificationResponder())
        sessions.append(session)
        return session

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM checkpoints WHERE authority_id = 'arun'"
            ).fetchone()[0]
        )
        for completed in payload["cursor"]["progress"]["completed"]:
            completed.pop("initial_request")
            completed.pop("expanded_request")
        connection.execute(
            "UPDATE checkpoints SET payload_json = ? WHERE authority_id = 'arun'",
            (json.dumps(payload, separators=(",", ":")),),
        )
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert all(
        item["initial_request"] is not None for item in receipt["query_inventory"]
    )
    assert sessions[-1].requested_urls == sessions[-2].requested_urls == ()


@pytest.mark.parametrize(
    ("tamper_sql", "failed_check"),
    [
        (
            """
            UPDATE semantic_versions
            SET payload_json = json_set(payload_json, '$.proposal', 'tampered')
            WHERE id = (
                SELECT current.version_id
                FROM section_current AS current
                JOIN applications AS application
                    ON application.id = current.application_id
                WHERE application.authority_id = 'arun'
                    AND current.section = 'application'
                ORDER BY application.reference
                LIMIT 1
            )
            """,
            "normalised-evidence-agreement",
        ),
        (
            """
            INSERT INTO suppression_corrections (
                application_id,
                suppressed,
                reason,
                corrected_at
            )
            SELECT id, 1, 'tampered', '2026-09-16T00:00:00+00:00'
            FROM applications
            WHERE authority_id = 'arun'
            ORDER BY reference
            LIMIT 1
            """,
            "normalised-evidence-agreement",
        ),
        (
            """
            UPDATE applications
            SET source_id = 'tampered-source'
            WHERE id = (
                SELECT id
                FROM applications
                WHERE authority_id = 'arun'
                ORDER BY reference
                LIMIT 1
            );
            UPDATE discovery_queue
            SET source_id = 'tampered-source'
            WHERE reference = (
                SELECT reference
                FROM applications
                WHERE source_id = 'tampered-source'
                LIMIT 1
            );
            UPDATE native_rebuild_inputs
            SET source_id = 'tampered-source'
            WHERE reference = (
                SELECT reference
                FROM applications
                WHERE source_id = 'tampered-source'
                LIMIT 1
            )
            """,
            "source-evidence-identity",
        ),
        (
            """
            UPDATE applications
            SET locator = 'https://example.test/tampered'
            WHERE id = (
                SELECT id
                FROM applications
                WHERE authority_id = 'arun'
                ORDER BY reference
                LIMIT 1
            );
            UPDATE discovery_queue
            SET locator = 'https://example.test/tampered'
            WHERE reference = (
                SELECT reference
                FROM applications
                WHERE locator = 'https://example.test/tampered'
                LIMIT 1
            );
            UPDATE native_rebuild_inputs
            SET locator = 'https://example.test/tampered'
            WHERE reference = (
                SELECT reference
                FROM applications
                WHERE locator = 'https://example.test/tampered'
                LIMIT 1
            )
            """,
            "source-evidence-identity",
        ),
    ],
)
def test_arun_qualification_rejects_normalised_state_tampering(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
    tamper_sql: str,
    failed_check: str,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "normalised-tampering"
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

    def factory() -> _Session:
        return _Session(_QualificationResponder())

    assert module.main(args, session_factory=factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.executescript(tamper_sql)
        connection.commit()

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed", error
    assert failed_check in error["failed_checks"]
    assert receipt_path.read_text(encoding="utf-8") == original


def test_arun_qualification_replaces_an_existing_receipt_atomically(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "replace"
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

    assert (
        module.main(
            args,
            session_factory=lambda: _Session(_QualificationResponder()),
        )
        == 0
    )
    capsys.readouterr()
    receipt_path = data_dir / "arun-qualification-v3.json"
    receipt_path.write_text("old receipt", encoding="utf-8")

    assert (
        module.main(
            [*args, "--resume"],
            session_factory=lambda: _Session(_QualificationResponder()),
        )
        == 0
    )
    replaced = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert replaced["schema_version"] == 3
    assert all(check["ok"] for check in replaced["checks"])
    assert not (data_dir / ".arun-qualification-v3.json.tmp").exists()


def test_arun_qualification_scopes_costs_and_allows_interrupted_history(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "scoped-history"
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
    assert (
        module.main(args, session_factory=lambda: _Session(_QualificationResponder()))
        == 0
    )
    first = json.loads(capsys.readouterr().out)

    store = SqliteStore(
        data_dir / "yimby.sqlite3", EvidenceStore(data_dir / "evidence")
    )
    foreign_run = store.begin_run(AuthorityId("foreign"))
    store.finish_run(
        foreign_run,
        AuthorityId("foreign"),
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=999,
                transferred_bytes=999_999,
                duration_ms=999,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.LIVE,
        ),
    )
    interrupted_run = store.begin_run(AuthorityId("arun"))
    store.finish_run(
        interrupted_run,
        AuthorityId("arun"),
        RunOutcome(
            status=RunStatus.INTERRUPTED,
            metrics=RunMetrics(
                request_count=5,
                transferred_bytes=500,
                duration_ms=5,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.LIVE,
        ),
    )
    store.close()

    assert (
        module.main(
            [*args, "--resume"],
            session_factory=lambda: _Session(_QualificationResponder()),
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["costs"]["bootstrap_total"]["request_count"] == (
        first["costs"]["bootstrap_total"]["request_count"] + 5
    )
    assert resumed["run_statuses"] == [
        "succeeded",
        "succeeded",
        "interrupted",
        "succeeded",
        "succeeded",
    ]


@pytest.mark.parametrize("evidence_kind", ["application", "search"])
def test_arun_qualification_does_not_publish_over_corrupt_evidence(
    tmp_path: "Path",
    capsys: pytest.CaptureFixture[str],
    evidence_kind: str,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "tampered"
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
    factory = lambda: _Session(_QualificationResponder())  # noqa: E731

    assert module.main(args, session_factory=factory) == 0
    receipt_path = data_dir / "arun-qualification-v3.json"
    original = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(original)
    digest = (
        receipt["evidence"]["application_digests"][0]
        if evidence_kind == "application"
        else receipt["search_form_evidence_digest"]
    )
    evidence_path = data_dir / "evidence" / digest[:2] / f"{digest}.gz"
    evidence_path.write_bytes(b"tampered")

    assert module.main([*args, "--resume"], session_factory=factory) == 1
    assert json.loads(capsys.readouterr().err)["error"] == (
        "runtime-failure" if evidence_kind == "application" else "qualification-failed"
    )
    assert receipt_path.read_text(encoding="utf-8") == original
