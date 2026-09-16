# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Arun live discovery and qualification contracts."""

import asyncio
from datetime import date, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import TYPE_CHECKING, cast

import pytest

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

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        body = self.responder(request)
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
        return 0

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return self._mode

    async def aclose(self) -> None:
        return None


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
