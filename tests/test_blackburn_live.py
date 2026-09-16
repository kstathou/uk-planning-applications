# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, SLF001

"""Live Citizen-portal boundaries owned by Blackburn with Darwen."""

from __future__ import annotations

import asyncio
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest
from pydantic import HttpUrl, ValidationError

import yimby.authorities.blackburn_with_darwen.adapter as blackburn
from yimby import DiscoveryWindow
from yimby.domain import EvidenceCapture, EvidenceDigest, TransportMode

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _capture(body: bytes) -> EvidenceCapture:
    return EvidenceCapture(
        url=HttpUrl("https://online.blackburn.gov.uk/planning/index.html"),
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

    async def search(
        self,
        query: blackburn.BlackburnQueryV1,
    ) -> EvidenceCapture:
        self.queries.append(query)
        return _capture(self.responder(query))

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
        _search_html("<tr><td>wrong columns</td></tr>"),
        _search_html(_result_row(1).replace("view_application", "wrong")),
        _search_html(_result_row(1).replace('data-id="178001"', 'data-id="bad"')),
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
