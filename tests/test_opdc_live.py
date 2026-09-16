# Copyright (c) 2026 Kostas Stathoulopoulos
"""OPDC Agile API and qualification boundary behaviour."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from yimby.authorities.opdc.adapter import (
    API_BASE_URL,
    OpdcAdapter,
    OpdcCheckpointV1,
    OpdcCompletedQuery,
    OpdcDiscoveryQuery,
    OpdcDiscoveryScope,
    OpdcIdentity,
    OpdcParseError,
)
from yimby.domain import (
    DiscoveryBatch,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    TransportMode,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalRequest


WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)
_LIVE_PAGE = "live"
_FIRST_PAGE = "first"
_COMPLETE_PAGE = "complete"


def _query_urls(window: DiscoveryWindow = WINDOW) -> tuple[str, ...]:
    return (
        (
            f"{API_BASE_URL}/api/application/search?"
            f"registrationDateFrom={window.start.isoformat()}&"
            f"registrationDateTo={window.end.isoformat()}&status=registered"
        ),
        (
            f"{API_BASE_URL}/api/application/search?"
            f"decisionDateFrom={window.start.isoformat()}&"
            f"decisionDateTo={window.end.isoformat()}&status=determined"
        ),
        f"{API_BASE_URL}/api/application/search?status=registered",
    )


def _search(*identities: tuple[int, str], total: int | None = None) -> bytes:
    results = [
        {"id": locator, "reference": reference, "proposal": "Published proposal"}
        for locator, reference in identities
    ]
    return json.dumps(
        {"total": len(results) if total is None else total, "results": results}
    ).encode()


class _OpdcSession:
    def __init__(
        self,
        responses: dict[str, bytes | Exception],
        *,
        mode: TransportMode = TransportMode.LIVE,
    ) -> None:
        self._responses = responses
        self._mode = mode
        self.requests: list[PortalRequest] = []
        self.closed = False

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        url = str(request.url)
        response = self._responses[url]
        self.requests.append(request)
        if isinstance(response, Exception):
            raise response
        return EvidenceCapture(
            url=request.url,
            media_type="application/json",
            body=response,
            digest=EvidenceDigest(sha256(response).hexdigest()),
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(str(request.url) for request in self.requests)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return sum(
            len(response)
            for request in self.requests
            if isinstance((response := self._responses[str(request.url)]), bytes)
        )

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return self._mode

    async def aclose(self) -> None:
        self.closed = True


async def _batches(
    session: _OpdcSession,
    *,
    window: DiscoveryWindow = WINDOW,
    checkpoint: OpdcCheckpointV1 | None = None,
) -> tuple[DiscoveryBatch[OpdcCheckpointV1], ...]:
    return tuple(
        [batch async for batch in OpdcAdapter().discover(session, window, checkpoint)]
    )


def _complete_responses(
    window: DiscoveryWindow = WINDOW,
) -> dict[str, bytes | Exception]:
    registered, determined, current = _query_urls(window)
    return {
        registered: _search((1, "26/0001/FULOPDC"), (2, "26/0002/FULOPDC")),
        determined: _search((2, "26/0002/FULOPDC"), (3, "26/0003/FULOPDC")),
        current: _search((1, "26/0001/FULOPDC"), (4, "15/0004/FULOPDC")),
    }


def test_opdc_live_discovery_exhausts_exact_full_array_queries() -> None:
    """The API's complete arrays implement the portal's client-side pages."""
    session = _OpdcSession(_complete_responses())
    batches = asyncio.run(_batches(session))

    assert tuple(session.requested_urls) == _query_urls()
    assert [
        [(reference.reference, reference.locator) for reference in batch.references]
        for batch in batches
    ] == [
        [("26/0001/FULOPDC", "1"), ("26/0002/FULOPDC", "2")],
        [("26/0003/FULOPDC", "3")],
        [("15/0004/FULOPDC", "4")],
    ]
    assert [batch.complete for batch in batches] == [False, False, True]
    final = batches[-1].next_checkpoint
    assert final.live_complete is True
    assert final.completed_queries == (
        OpdcCompletedQuery(
            query=OpdcDiscoveryQuery.REGISTERED_WINDOW,
            result_total=2,
        ),
        OpdcCompletedQuery(
            query=OpdcDiscoveryQuery.DETERMINED_WINDOW,
            result_total=2,
        ),
        OpdcCompletedQuery(
            query=OpdcDiscoveryQuery.REGISTERED_OPEN,
            result_total=2,
        ),
    )
    assert final.seen_references == (
        OpdcIdentity(reference="26/0001/FULOPDC", locator="1"),
        OpdcIdentity(reference="26/0002/FULOPDC", locator="2"),
        OpdcIdentity(reference="26/0003/FULOPDC", locator="3"),
        OpdcIdentity(reference="15/0004/FULOPDC", locator="4"),
    )
    assert all(
        request.method == "GET" and not request.form for request in session.requests
    )
    assert all(
        [(header.name, header.value) for header in request.headers]
        == [
            ("x-client", "OPDC"),
            ("x-product", "CITIZENPORTAL"),
            ("x-service", "PA"),
        ]
        for request in session.requests
    )


def test_opdc_live_discovery_resumes_restarts_scope_and_reruns_without_io() -> None:
    """Committed query prefixes resume, changed scopes restart, and terminals stop."""

    async def first_query() -> DiscoveryBatch[OpdcCheckpointV1]:
        registered = _query_urls()[0]
        iterator: AsyncIterator[DiscoveryBatch[OpdcCheckpointV1]] = (
            OpdcAdapter().discover(
                _OpdcSession({registered: _search((1, "26/0001/FULOPDC"))}),
                WINDOW,
                None,
            )
        )
        first = await anext(iterator)
        await iterator.aclose()
        return first

    checkpoint = asyncio.run(first_query()).next_checkpoint
    remaining_responses = _complete_responses()
    remaining_responses.pop(_query_urls()[0])
    resumed = _OpdcSession(remaining_responses)
    resumed_batches = asyncio.run(_batches(resumed, checkpoint=checkpoint))
    assert resumed.requested_urls == _query_urls()[1:]
    assert resumed_batches[-1].complete is True

    terminal = resumed_batches[-1].next_checkpoint
    rerun = _OpdcSession({})
    assert asyncio.run(_batches(rerun, checkpoint=terminal))[0].references == ()
    assert rerun.requested_urls == ()

    changed = WINDOW.model_copy(update={"start": date(2026, 8, 19)})
    restarted = _OpdcSession(_complete_responses(changed))
    restarted_batches = asyncio.run(
        _batches(restarted, window=changed, checkpoint=terminal)
    )
    assert restarted.requested_urls == _query_urls(changed)
    assert restarted_batches[-1].next_checkpoint.live_scope == OpdcDiscoveryScope(
        start=changed.start,
        end=changed.end,
        include_open=True,
    )


def test_opdc_live_discovery_can_exclude_the_open_query() -> None:
    """A bounded-only caller runs exactly the two dated portal queries."""
    window = WINDOW.model_copy(update={"include_open": False})
    registered, determined, _ = _query_urls(window)
    session = _OpdcSession(
        {
            registered: _search((1, "26/0001/FULOPDC")),
            determined: _search(),
        }
    )
    batches = asyncio.run(_batches(session, window=window))

    assert session.requested_urls == (registered, determined)
    assert batches[-1].complete is True
    assert tuple(
        item.query for item in batches[-1].next_checkpoint.completed_queries
    ) == (
        OpdcDiscoveryQuery.REGISTERED_WINDOW,
        OpdcDiscoveryQuery.DETERMINED_WINDOW,
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"{}",
        _search((1, "26/0001/FULOPDC"), total=2),
        _search((1, "26/0001/FULOPDC"), (1, "26/0002/FULOPDC")),
        _search((1, "26/0001/FULOPDC"), (2, "26/0001/FULOPDC")),
    ],
)
def test_opdc_live_discovery_rejects_malformed_or_incoherent_results(
    body: bytes,
) -> None:
    """Malformed wrappers, false totals, and duplicate identities fail closed."""
    session = _OpdcSession({_query_urls()[0]: body})
    with pytest.raises(OpdcParseError):
        asyncio.run(_batches(session))


def test_opdc_live_checkpoint_and_cross_query_identity_must_be_coherent() -> None:
    """Progress is an exact query prefix with a bijective public identity map."""
    scope = OpdcDiscoveryScope(
        start=WINDOW.start,
        end=WINDOW.end,
        include_open=True,
    )
    fixture_checkpoint = OpdcCheckpointV1(page_token=_COMPLETE_PAGE)
    assert fixture_checkpoint.live_complete is False
    with pytest.raises(ValueError, match="fixture/live state"):
        OpdcCheckpointV1(page_token=_LIVE_PAGE)
    with pytest.raises(ValueError, match="live page token"):
        OpdcCheckpointV1(page_token=_FIRST_PAGE, live_scope=scope)
    with pytest.raises(ValueError, match="query inventory"):
        OpdcCheckpointV1(
            page_token=_LIVE_PAGE,
            live_scope=scope,
            completed_queries=(
                OpdcCompletedQuery(
                    query=OpdcDiscoveryQuery.DETERMINED_WINDOW,
                    result_total=0,
                ),
            ),
        )
    with pytest.raises(ValueError, match="identity"):
        OpdcCheckpointV1(
            page_token=_LIVE_PAGE,
            live_scope=scope,
            seen_references=(
                OpdcIdentity(reference="A", locator="1"),
                OpdcIdentity(reference="A", locator="2"),
            ),
        )

    registered, determined, _ = _query_urls()
    session = _OpdcSession(
        {
            registered: _search((1, "26/0001/FULOPDC")),
            determined: _search((2, "26/0001/FULOPDC")),
        }
    )
    with pytest.raises(OpdcParseError, match="identity"):
        asyncio.run(_batches(session))

    fixture_url = (
        "https://planning.agileapplications.co.uk/opdc/search?"
        "from=2026-08-18&to=2026-09-16&page=first"
    )
    fixture = _OpdcSession(
        {
            fixture_url: (
                b'<article data-opdc-reference="A"></article>'
                b'<span data-opdc-page="complete"></span>'
            )
        },
        mode=TransportMode.FIXTURE,
    )
    live_progress = OpdcCheckpointV1(page_token=_LIVE_PAGE, live_scope=scope)
    fixture_batches = asyncio.run(_batches(fixture, checkpoint=live_progress))
    assert fixture.requested_urls == (fixture_url,)
    assert fixture_batches[0].complete is True
