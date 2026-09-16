# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, SLF001

"""Live Citizen-portal boundaries owned by Blackburn with Darwen."""

from __future__ import annotations

import asyncio
from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import HttpUrl, ValidationError

import yimby.authorities.blackburn_with_darwen.adapter as blackburn
from yimby import DiscoveryWindow
from yimby.domain import (
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from yimby.transport import PortalSession


WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)


def _query(
    kind: blackburn.BlackburnQueryKind,
    start: date,
    end: date,
) -> blackburn.BlackburnQueryV1:
    return blackburn.BlackburnQueryV1(
        kind=kind,
        date_range=blackburn.BlackburnDateRangeV1(start=start, end=end),
    )


def _result_row(number: int, *, decision: str = "") -> str:
    return f"""
    <tr>
      <td>10/26/{number:04d}</td><td>Full Planning Application</td>
      <td>{number} High Street</td><td>Build home {number}</td>
      <td>Blackburn Central</td><td>Blackburn</td><td>{decision}</td>
      <td><button class="view_application" data-id="{178000 + number}">
      View</button></td>
    </tr>
    """


def _search_html(*rows: str) -> bytes:
    return f"""
    <html><body>
      <table id="application_results_table">
        <thead><tr><th>Application Reference</th><th>Application Type</th>
        <th>Location Details</th><th>Proposal</th><th>Ward</th>
        <th>Community</th><th>Decision</th><th>View</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </body></html>
    """.encode()


def _capture(
    body: bytes,
    url: str = "https://online.blackburn.gov.uk/planning/index.html",
) -> EvidenceCapture:
    return EvidenceCapture(
        url=HttpUrl(url),
        media_type="text/html",
        body=body,
        digest=EvidenceDigest(sha256(body).hexdigest()),
    )


class _BlackburnSession:
    def __init__(
        self,
        responder: Callable[[blackburn.BlackburnQueryV1], bytes],
    ) -> None:
        self.responder = responder
        self.queries: list[blackburn.BlackburnQueryV1] = []
        self.application_body: bytes | None = None
        self.application_calls: list[blackburn.BlackburnLocatorV1] = []

    async def search(
        self,
        query: blackburn.BlackburnQueryV1,
    ) -> EvidenceCapture:
        self.queries.append(query)
        return _capture(self.responder(query))

    async def application(
        self,
        locator: blackburn.BlackburnLocatorV1,
    ) -> EvidenceCapture:
        self.application_calls.append(locator)
        if self.application_body is None:
            raise AssertionError
        return _capture(
            self.application_body,
            f"https://online.blackburn.gov.uk/planning/index.html"
            f"?fa=getApplication&id={locator.record_id}",
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(
            "https://online.blackburn.gov.uk/planning/index.html" for _ in self.queries
        )

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
        return TransportMode.BROWSER

    async def fetch(self, *_args: object, **_kwargs: object) -> EvidenceCapture:
        raise AssertionError

    async def aclose(self) -> None:
        return None


def test_blackburn_query_inventory_splits_caps_and_filters_older_open() -> None:
    root_received = _query(
        blackburn.BlackburnQueryKind.RECEIVED,
        WINDOW.start,
        WINDOW.end,
    )
    first_received = _query(
        blackburn.BlackburnQueryKind.RECEIVED,
        date(2026, 8, 18),
        date(2026, 9, 1),
    )
    second_received = _query(
        blackburn.BlackburnQueryKind.RECEIVED,
        date(2026, 9, 2),
        date(2026, 9, 16),
    )
    valid = _query(blackburn.BlackburnQueryKind.VALID, WINDOW.start, WINDOW.end)
    decision = _query(
        blackburn.BlackburnQueryKind.DECISION,
        WINDOW.start,
        WINDOW.end,
    )
    older_open = _query(
        blackburn.BlackburnQueryKind.OLDER_OPEN,
        date(1977, 1, 1),
        date(2026, 8, 17),
    )
    responses = {
        root_received.key: _search_html(
            *(_result_row(number) for number in range(1, 31))
        ),
        first_received.key: _search_html(_result_row(1)),
        second_received.key: _search_html(_result_row(2)),
        valid.key: _search_html(_result_row(1)),
        decision.key: _search_html(_result_row(3, decision="GRANT")),
        older_open.key: _search_html(
            _result_row(4),
            _result_row(5, decision="GRANT"),
        ),
    }
    session = _BlackburnSession(lambda query: responses[query.key])
    adapter = blackburn.BlackburnWithDarwenAdapter()

    async def exercise() -> None:
        batches = [batch async for batch in adapter.discover(session, WINDOW, None)]
        terminal = batches[-1].next_checkpoint
        assert batches[-1].complete
        assert terminal.live_complete
        assert terminal.pending_queries == ()
        assert terminal.completed_queries == (
            first_received,
            second_received,
            valid,
            decision,
            older_open,
        )
        assert terminal.split_queries == (root_received,)
        assert terminal.seen_references == (
            "10/26/0001",
            "10/26/0002",
            "10/26/0003",
            "10/26/0004",
        )
        assert session.queries == [
            root_received,
            first_received,
            second_received,
            valid,
            decision,
            older_open,
        ]
        assert [
            reference.reference for batch in batches for reference in batch.references
        ] == ["10/26/0001", "10/26/0002", "10/26/0003", "10/26/0004"]

        requests_before = len(session.queries)
        rerun = [batch async for batch in adapter.discover(session, WINDOW, terminal)]
        assert len(rerun) == 1
        assert rerun[0].complete
        assert rerun[0].references == ()
        assert len(session.queries) == requests_before

    asyncio.run(exercise())


def _detail_row(label: str, value: str) -> str:
    return f"""
    <div class="row pad-bottom-5">
      <div class="col-md-5"><strong>{label}:</strong></div>
      <div class="col-md-7">{value}</div>
    </div>
    """


def _detail_html(reference: str = "10/26/0747", *, documents: int = 2) -> bytes:
    fields = (
        ("Application Reference Number", reference),
        ("Application Type", "Full Planning Application"),
        ("Proposal", "Change use &amp; provide 14 retail units"),
        ("Applicant", "Applicant One"),
        ("Agent", "Agent One"),
        ("Location", "Q Lounge, Blackburn, BB2 2HB"),
        ("Grid Reference", "368232, 427893"),
        ("Ward", "Blackburn Central"),
        ("Parish / Community", "Blackburn"),
        ("Officer", "Officer One"),
        ("Decision Level", "Delegated"),
        ("Application Status", "Pending Consideration"),
        ("Received Date", "21-08-2026"),
        ("Valid Date", "15-09-2026"),
        ("Expiry Date", "10-11-2026"),
        ("Extension Of Time", "No"),
        ("Extension Of Time Due Date", ""),
        ("Planning Performance Agreement", "No"),
        ("Planning Performance Agreement Due Date", ""),
        ("Proposed Committee Date", ""),
        ("Actual Committee Date", ""),
        ("Decision Issued Date", ""),
        ("Decision", ""),
        ("Appeal Reference", ""),
        ("Appeal Status", ""),
        ("Appeal External Decision", ""),
        ("Appeal External Decision Date", ""),
    )
    rows = "".join(
        f"""
        <tr>
          <td data-field-name="document_type">Plan</td>
          <td data-field-name="description">Drawing {index}</td>
          <td data-field-name="thumbnail"><img src="https://cdn.test/thumb.png"></td>
          <td data-field-name="date_document_added"
              data-date-value="2026-09-{10 + index:02d}">{10 + index}-09-2026</td>
          <td data-field-name="download"><a
            href="/planning/?fa=downloadDocument&amp;id={227500 + index}"
            >Download</a></td>
        </tr>
        """
        for index in range(1, documents + 1)
    )
    return f"""
    <div id="application_details" data-application-id="178041">
      {"".join(_detail_row(label, value) for label, value in fields)}
    </div>
    <table id="application_documents">
      <thead><tr>
        <th data-field-name="document_type">Document Type</th>
        <th data-field-name="description">Description</th>
        <th data-field-name="thumbnail">Thumbnail</th>
        <th data-field-name="date_document_added">Date Document Added</th>
        <th data-field-name="download">Download/View</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """.encode()


def test_blackburn_live_detail_keeps_document_metadata_only() -> None:
    locator = blackburn.BlackburnLocatorV1(
        record_id="178041",
        public_reference="10/26/0747",
    )
    reference = SourceReference(
        source_id=blackburn.SOURCE,
        reference=locator.public_reference,
        locator=locator.model_dump_json(),
    )
    session = _BlackburnSession(lambda _query: _search_html())
    session.application_body = _detail_html()
    adapter = blackburn.BlackburnWithDarwenAdapter()

    snapshot = asyncio.run(adapter.fetch(session, reference))
    observation = adapter.normalise(snapshot)

    assert snapshot.payload.record_id == "178041"
    assert snapshot.payload.received_date == date(2026, 8, 21)
    assert snapshot.payload.valid_date == date(2026, 9, 15)
    assert [document.title for document in snapshot.payload.documents] == [
        "Drawing 1",
        "Drawing 2",
    ]
    assert snapshot.completeness.documents.kind == "complete"
    assert snapshot.completeness.documents.item_count == len(snapshot.payload.documents)
    assert snapshot.completeness.comments.kind == "unavailable"
    assert observation.proposal == "Change use & provide 14 retail units"
    assert observation.status == "pending-consideration"
    assert observation.metadata.address == "Q Lounge, Blackburn, BB2 2HB"
    assert observation.metadata.source_url == snapshot.evidence[0].url
    assert tuple(str(document.url) for document in observation.documents) == (
        "https://online.blackburn.gov.uk/planning/?fa=downloadDocument&id=227501",
        "https://online.blackburn.gov.uk/planning/?fa=downloadDocument&id=227502",
    )
    assert session.application_calls == [locator]
    assert session.attachment_body_requests == 0


def test_blackburn_detail_requires_locator_agreement_and_document_shape() -> None:
    locator = blackburn.BlackburnLocatorV1(
        record_id="178041",
        public_reference="10/26/0747",
    )
    reference = SourceReference(
        source_id=blackburn.SOURCE,
        reference=locator.public_reference,
        locator=locator.model_dump_json(),
    )
    adapter = blackburn.BlackburnWithDarwenAdapter()

    mismatch = _BlackburnSession(lambda _query: _search_html())
    mismatch.application_body = _detail_html("10/26/9999")
    with pytest.raises(blackburn.BlackburnReferenceMismatchError):
        asyncio.run(adapter.fetch(mismatch, reference))

    malformed = _BlackburnSession(lambda _query: _search_html())
    malformed.application_body = _detail_html().replace(
        b'data-field-name="description"',
        b'data-field-name="changed"',
        1,
    )
    with pytest.raises(blackburn.BlackburnWithDarwenParseError):
        asyncio.run(adapter.fetch(malformed, reference))

    for bad_reference in (
        reference.model_copy(update={"source_id": "wrong"}),
        reference.model_copy(update={"locator": None}),
        reference.model_copy(update={"locator": "not-json"}),
    ):
        with pytest.raises(blackburn.BlackburnRoutingError):
            asyncio.run(adapter.fetch(malformed, bad_reference))


def test_blackburn_detail_confirms_an_empty_document_table() -> None:
    locator = blackburn.BlackburnLocatorV1(
        record_id="178041",
        public_reference="10/26/0747",
    )
    reference = SourceReference(
        source_id=blackburn.SOURCE,
        reference=locator.public_reference,
        locator=locator.model_dump_json(),
    )
    session = _BlackburnSession(lambda _query: _search_html())
    session.application_body = _detail_html(documents=0)

    snapshot = asyncio.run(
        blackburn.BlackburnWithDarwenAdapter().fetch(session, reference)
    )

    assert snapshot.payload.documents == ()
    assert snapshot.completeness.documents.kind == "empty"


def test_blackburn_single_day_at_result_cap_fails_closed() -> None:
    one_day = DiscoveryWindow(
        start=date(2026, 9, 16),
        end=date(2026, 9, 16),
        include_open=False,
    )
    session = _BlackburnSession(
        lambda _query: _search_html(*(_result_row(number) for number in range(1, 31)))
    )

    async def exercise() -> None:
        with pytest.raises(blackburn.BlackburnResultCapError):
            async for _batch in blackburn.BlackburnWithDarwenAdapter().discover(
                session,
                one_day,
                None,
            ):
                pass

    asyncio.run(exercise())


def test_blackburn_search_requires_an_explicit_empty_marker() -> None:
    assert blackburn._parse_search_rows(b"<strong>No Results Found.</strong>") == ()
    with pytest.raises(blackburn.BlackburnWithDarwenParseError):
        blackburn._parse_search_rows(b"<html><body></body></html>")


def test_blackburn_query_types_reject_invalid_ranges() -> None:
    with pytest.raises(ValidationError):
        blackburn.BlackburnDateRangeV1(
            start=date(2026, 9, 16),
            end=date(2026, 9, 15),
        )
    query = _query(
        blackburn.BlackburnQueryKind.RECEIVED,
        date(2026, 8, 18),
        date(2026, 9, 16),
    )
    assert query.key == "received|2026-08-18|2026-09-16"


@pytest.mark.parametrize(
    "body",
    [
        b'<table id="application_results_table"></table>',
        b'<table id="application_results_table"></table>' * 2,
        _search_html("<tr><td>wrong columns</td></tr>"),
        _search_html(_result_row(1).replace("view_application", "wrong")),
        _search_html(_result_row(1).replace('data-id="178001"', 'data-id="bad"')),
        _search_html(_result_row(1).replace("Build home 1", "")),
    ],
)
def test_blackburn_search_rows_fail_closed_on_shape_drift(body: bytes) -> None:
    with pytest.raises(blackburn.BlackburnWithDarwenParseError):
        blackburn._parse_search_rows(body)


def test_blackburn_search_rejects_duplicates_and_more_than_the_observed_cap() -> None:
    duplicate = _result_row(1)
    with pytest.raises(blackburn.BlackburnDuplicateReferenceError):
        blackburn._parse_search_rows(_search_html(duplicate, duplicate))
    with pytest.raises(blackburn.BlackburnResultCountError):
        blackburn._parse_search_rows(
            _search_html(*(_result_row(number) for number in range(1, 32)))
        )


def test_blackburn_live_discovery_requires_the_page_session() -> None:
    session = cast(
        "PortalSession",
        SimpleNamespace(mode=TransportMode.LIVE),
    )

    async def exercise() -> None:
        with pytest.raises(blackburn.BlackburnBrowserSessionRequiredError):
            async for _batch in blackburn.BlackburnWithDarwenAdapter().discover(
                session,
                WINDOW,
                None,
            ):
                pass

    asyncio.run(exercise())
