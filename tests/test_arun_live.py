# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Arun live discovery and qualification contracts."""

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import HttpUrl

import yimby.authorities.arun.adapter as arun
from yimby.domain import (
    DiscoveryBatch,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
)
from yimby.transport import FormField, PortalRequest, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path
    from types import ModuleType


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
    <table><tr><th>Reference</th></tr>
      <tr><td><a href="planningDetails?reference=BR/1/26/PL&amp;from=planningSearch">
        BR/1/26/PL</a></td></tr>
    </table>
    <strong>First 20 results shown, there are 2 in total</strong>
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
        f'{reference}&amp;from=planningSearch">{reference}</a></td></tr>'
        for reference in references
    )
    return f"<table><tr><th>Reference</th></tr>{rows}</table>".encode()


def _detail_with_documents(reference: str) -> bytes:
    return f"""
    <table>
      <tr><th>Reference</th><td>{reference}</td></tr>
      <tr><th>Proposal</th><td>Build one home</td></tr>
      <tr><th>Status</th><td>Undecided</td></tr>
      <tr><th>Parish</th><td>Bognor Regis</td></tr>
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
        return b"No applications found for entered search criteria"


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

    empty = arun._parse_search_results(
        b"No applications found for entered search criteria"
    )
    assert empty.reported == 0
    assert empty.references == ()

    duplicate = (
        b'<table><tr><td><a href="planningDetails?reference=A">A</a></td></tr>'
        b'<tr><td><a href="planningDetails?reference=A">A again</a></td></tr></table>'
        b'<p data-result-count="2">2 records</p>'
    )
    with pytest.raises(arun.ArunParseError, match="duplicate result reference"):
        arun._parse_search_results(duplicate)


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
    assert [reference.reference for reference in first.references] == ["BR/1/26/PL"]
    assert isinstance(first.next_checkpoint.cursor, arun.ArunLiveCursor)
    assert isinstance(first.next_checkpoint.cursor.progress, arun.ArunAwaitingShowAll)

    resumed_session = _Session(_DiscoveryResponder())
    resumed = asyncio.run(
        _batches(adapter, resumed_session, window, first.next_checkpoint)
    )
    assert [
        reference.reference for batch in resumed for reference in batch.references
    ] == ["BR/2/26/PL"]
    terminal = resumed[-1].next_checkpoint
    assert resumed[-1].complete
    assert isinstance(terminal.cursor, arun.ArunLiveCursor)
    assert isinstance(terminal.cursor.progress, arun.ArunComplete)
    assert len(terminal.cursor.progress.completed) == 60
    assert terminal.cursor.progress.seen_references == _DiscoveryResponder.references

    terminal_session = _Session(_DiscoveryResponder())
    rerun = asyncio.run(_batches(adapter, terminal_session, window, terminal))
    assert len(rerun) == 1
    assert rerun[0].complete
    assert rerun[0].references == ()
    assert terminal_session.requests == []


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
    )

    with pytest.raises(ValueError, match="counts disagree"):
        arun.ArunCompletedQuery(
            key=plan[0].key,
            reported_count=1,
            enumerated_count=0,
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
    with pytest.raises(ValueError, match="absent from the seen set"):
        arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunAwaitingShowAll(
                next_query=0,
                reported_count=2,
                initial_references=("A",),
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
        arun._parse_search_results(b'<p data-result-count="200">200 records</p>')
    with pytest.raises(arun.ArunParseError, match="show all form"):
        arun._parse_search_results(_partial_results(fields) + _partial_results(fields))
    with pytest.raises(arun.ArunParseError, match="show all form method"):
        arun._parse_search_results(
            _partial_results(fields).replace(b'method="post"', b'method="get"')
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
    replay = arun.ArunCheckpointV1(
        cursor=arun.ArunLiveCursor(
            scope=scope,
            plan=plan,
            progress=arun.ArunAwaitingShowAll(
                next_query=0,
                reported_count=2,
                initial_references=("DIFFERENT",),
                seen_references=("DIFFERENT",),
            ),
        )
    )
    with pytest.raises(arun.ArunQueryReplayError):
        asyncio.run(_batches(adapter, _Session(_DiscoveryResponder()), window, replay))

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

    with pytest.raises(arun.ArunQueryReplayError):
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
                )
                for query in plan
            )
        ),
    )
    with pytest.raises(arun.ArunCheckpointError):
        arun._complete_query(terminal, plan[0], 0, ())


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
        return b"No documents found for this planning application"

    snapshot = asyncio.run(arun.ArunAdapter().fetch(_Session(responder), reference))

    assert snapshot.payload.parish_name is None
    assert snapshot.completeness.documents.kind == "empty"


def test_arun_document_action_and_index_fail_closed_on_ambiguous_shapes() -> None:
    detail = _detail_with_documents("BR/1/26/PL")
    with pytest.raises(arun.ArunParseError, match="document action reference"):
        arun._document_request(detail, "OTHER/1")
    with pytest.raises(arun.ArunParseError, match="document action"):
        arun._document_request(detail + detail, "BR/1/26/PL")
    with pytest.raises(arun.ArunParseError, match="document pagination"):
        arun._parse_document_index(
            _document_index() + b'<nav class="pagination">next</nav>'
        )
    with pytest.raises(arun.ArunParseError, match="document filter"):
        arun._parse_document_index(
            _document_index().replace(b'value="" selected', b'value="PLAN" selected')
        )
    documents = arun._parse_document_index(
        b"No documents found for this planning application"
    )
    assert documents == ()


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
        arun._parse_document_index(b"unknown document response")
    with pytest.raises(arun.ArunParseError, match="document table"):
        arun._parse_document_index(_document_index() + _document_index())
    with pytest.raises(arun.ArunParseError, match="document row"):
        arun._parse_document_index(
            b"<table><tr><th>Type</th><th>Date</th></tr><tr><td>Only</td></tr></table>"
        )
    with pytest.raises(arun.ArunParseError, match="document link"):
        arun._parse_document_index(
            b"<table><tr><th>Type</th><th>Date</th></tr>"
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


def test_arun_qualification_receipt_proves_exact_state_and_zero_io_rerun(
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
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == 66
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
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
    assert receipt["query_inventory"][-1]["query"]["end"] == "2026-09-16"
    assert receipt["references"] == {
        "discovered": ["BR/1/26/PL", "BR/2/26/PL"],
        "retained_native": ["BR/1/26/PL", "BR/2/26/PL"],
        "applications": ["BR/1/26/PL", "BR/2/26/PL"],
    }
    assert receipt["evidence"]["capture_count"] == 4
    assert len(receipt["semantic_fingerprint"]) == 64
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
    assert all(check["ok"] for check in receipt["checks"])
    receipt_path = data_dir / "arun-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert not (data_dir / ".arun-qualification-v1.json.tmp").exists()
