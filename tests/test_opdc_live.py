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
    SOURCE,
    OpdcAdapter,
    OpdcCheckpointV1,
    OpdcCompletedQuery,
    OpdcDiscoveryQuery,
    OpdcDiscoveryScope,
    OpdcDocumentV1,
    OpdcIdentity,
    OpdcParseError,
    OpdcResponseV1,
)
from yimby.domain import (
    DiscoveryBatch,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
)
from yimby.transport import SourceUnavailableError

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
_EASTING = 521000
_NORTHING = 182000
_DETAIL_EVIDENCE_COUNT = 3


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


def _detail_url(locator: str, suffix: str = "") -> str:
    return f"{API_BASE_URL}/api/application/{locator}{suffix}"


def _detail_body(
    *,
    locator: int = 10008,
    reference: str = "23/0014/FUMOPDC",
) -> bytes:
    return json.dumps(
        {
            "id": locator,
            "reference": reference,
            "oneAppReference": "PP-11999999",
            "webReference": reference,
            "fullProposal": "Build homes and workspace",
            "proposal": "Build homes",
            "statusOwner": "Decision issued",
            "location": "Old Oak Common Lane, London",
            "applicationType": "Full planning application",
            "decisionText": "Approved",
            "receivedDate": "2023-01-10T00:00:00",
            "registrationDate": "2023-01-12T00:00:00",
            "validDate": "2023-01-11T00:00:00",
            "decisionDate": "2024-02-01T00:00:00",
            "ward": "College Park and Old Oak",
            "easting": _EASTING,
            "northing": _NORTHING,
            "applicantEmail": "not-retained@example.test",
        }
    ).encode()


def _documents_body(*, duplicate: bool = False, invalid_date: bool = False) -> bytes:
    documents = [
        {
            "documentId": "DOC-1",
            "description": "Committee report",
            "name": "report.pdf",
            "mediaDescription": "Report",
            "receivedDate": ("not-a-date" if invalid_date else "2024-01-30T00:00:00"),
            "documentHash": "public-hash",
            "mediaId": 1,
        }
    ]
    if duplicate:
        documents.append({**documents[0], "description": "Duplicate"})
    return json.dumps(documents).encode()


def _responses_body(*, duplicate: bool = False) -> bytes:
    responses = [
        {
            "replyId": 501,
            "replyLongText": "I support the additional homes.",
            "replyDate": "2023-03-02T00:00:00",
            "replyType": "Support",
            "name": "Respondent",
            "linkedDocuments": 0,
        }
    ]
    if duplicate:
        responses.append({**responses[0], "replyLongText": "Duplicate"})
    return json.dumps(responses).encode()


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


def test_opdc_live_detail_retains_metadata_comments_and_three_evidence_captures() -> (
    None
):
    """One routed record preserves every implemented public API section."""
    reference = SourceReference(
        source_id=SOURCE,
        reference="23/0014/FUMOPDC",
        locator="10008",
    )
    session = _OpdcSession(
        {
            _detail_url("10008"): _detail_body(),
            _detail_url("10008", "/document"): _documents_body(),
            _detail_url("10008", "/responses"): _responses_body(),
        }
    )
    adapter = OpdcAdapter()
    snapshot = asyncio.run(adapter.fetch(session, reference))

    document_url = f"{API_BASE_URL}/api/application/document/OPDC/DOC-1"
    assert snapshot.payload.agile_case_id == "10008"
    assert snapshot.payload.application_reference == reference.reference
    assert snapshot.payload.documents == (
        OpdcDocumentV1(
            document_id="DOC-1",
            title="Committee report",
            url=document_url,
            received_date=date(2024, 1, 30),
            media_description="Report",
            source_name="report.pdf",
        ),
    )
    assert snapshot.payload.responses == (
        OpdcResponseV1(
            response_id="501",
            text="I support the additional homes.",
            received_date=date(2023, 3, 2),
            response_type="Support",
        ),
    )
    assert snapshot.completeness.application.kind == "complete"
    assert snapshot.completeness.documents.kind == "complete"
    assert snapshot.completeness.comments.kind == "complete"
    assert [str(capture.url) for capture in snapshot.evidence] == [
        _detail_url("10008"),
        _detail_url("10008", "/document"),
        _detail_url("10008", "/responses"),
    ]
    assert document_url not in session.requested_urls
    assert all(
        [(header.name, header.value) for header in request.headers]
        == [
            ("x-client", "OPDC"),
            ("x-product", "CITIZENPORTAL"),
            ("x-service", "PA"),
        ]
        for request in session.requests
    )

    normalised = adapter.normalise(snapshot)
    assert normalised.proposal == "Build homes and workspace"
    assert normalised.status == "decision-issued"
    assert normalised.documents[0].title == "Committee report"
    assert str(normalised.documents[0].url) == document_url
    assert normalised.comments[0].text == "I support the additional homes."
    assert normalised.metadata.aliases == ("PP-11999999",)
    assert normalised.metadata.application_type == "Full planning application"
    assert normalised.metadata.decision == "Approved"
    assert normalised.metadata.address == "Old Oak Common Lane, London"
    assert normalised.metadata.received_date == date(2023, 1, 10)
    assert normalised.metadata.validated_date == date(2023, 1, 11)
    assert normalised.metadata.decision_date == date(2024, 2, 1)
    assert normalised.metadata.location is not None
    assert normalised.metadata.location.bng_easting == _EASTING
    assert normalised.metadata.location.bng_northing == _NORTHING


def test_opdc_live_detail_marks_verified_empty_child_arrays() -> None:
    """A successfully parsed empty array is distinct from a failed section."""
    reference = SourceReference(
        source_id=SOURCE,
        reference="23/0014/FUMOPDC",
        locator="10008",
    )
    session = _OpdcSession(
        {
            _detail_url("10008"): _detail_body(),
            _detail_url("10008", "/document"): b"[]",
            _detail_url("10008", "/responses"): b"[]",
        }
    )
    snapshot = asyncio.run(OpdcAdapter().fetch(session, reference))

    assert snapshot.payload.documents == ()
    assert snapshot.payload.responses == ()
    assert snapshot.completeness.documents.kind == "empty"
    assert snapshot.completeness.comments.kind == "empty"
    assert len(snapshot.evidence) == _DETAIL_EVIDENCE_COUNT


@pytest.mark.parametrize(
    ("documents", "responses"),
    [
        (SourceUnavailableError("documents unavailable"), b"not-json"),
        (_documents_body(duplicate=True), _responses_body(duplicate=True)),
        (_documents_body(invalid_date=True), b"[]"),
    ],
)
def test_opdc_live_detail_keeps_child_failures_explicit(
    documents: bytes | Exception,
    responses: bytes | Exception,
) -> None:
    """Unavailable or malformed child arrays cannot become false empties."""
    reference = SourceReference(
        source_id=SOURCE,
        reference="23/0014/FUMOPDC",
        locator="10008",
    )
    session = _OpdcSession(
        {
            _detail_url("10008"): _detail_body(),
            _detail_url("10008", "/document"): documents,
            _detail_url("10008", "/responses"): responses,
        }
    )
    snapshot = asyncio.run(OpdcAdapter().fetch(session, reference))

    assert snapshot.completeness.documents.kind == "failed"
    assert snapshot.payload.documents == ()
    if responses == b"[]":
        assert snapshot.completeness.comments.kind == "empty"
    else:
        assert snapshot.completeness.comments.kind == "failed"
        assert snapshot.payload.responses == ()


def test_opdc_live_detail_requires_locator_shape_and_identity_agreement() -> None:
    """Discovery identity routes and authenticates the returned application."""
    adapter = OpdcAdapter()
    missing = SourceReference(
        source_id=SOURCE,
        reference="23/0014/FUMOPDC",
    )
    with pytest.raises(ValueError, match="locator"):
        asyncio.run(adapter.fetch(_OpdcSession({}), missing))

    reference = missing.model_copy(update={"locator": "10008"})
    mismatched = _OpdcSession({_detail_url("10008"): _detail_body(reference="OTHER")})
    with pytest.raises(ValueError, match="reference mismatch"):
        asyncio.run(adapter.fetch(mismatched, reference))

    malformed = _OpdcSession({_detail_url("10008"): b"{}"})
    with pytest.raises(OpdcParseError, match="detail JSON"):
        asyncio.run(adapter.fetch(malformed, reference))
