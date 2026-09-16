# Copyright (c) 2026 Kostas Stathoulopoulos
"""OPDC Agile API and qualification boundary behaviour."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import HttpUrl

from yimby.authorities.opdc import adapter as opdc_adapter
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
    ApplicationLocation,
    DiscoveryBatch,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
    Wgs84Coordinate,
)
from yimby.transport import SourceUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import ModuleType

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
_CONFIG_ERROR = 2
_QUALIFICATION_SESSION_COUNT = 2
_QUALIFICATION_REQUEST_COUNT = 15
_QUALIFICATION_APPLICATION_COUNT = 4


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


def _updated_detail(**updates: object) -> bytes:
    payload = json.loads(_detail_body())
    payload.update(updates)
    return json.dumps(payload).encode()


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


def _qualification_responses() -> dict[str, bytes | Exception]:
    responses = _complete_responses()
    for locator, reference in (
        (1, "26/0001/FULOPDC"),
        (2, "26/0002/FULOPDC"),
        (3, "26/0003/FULOPDC"),
        (4, "15/0004/FULOPDC"),
    ):
        value = str(locator)
        responses[_detail_url(value)] = _detail_body(
            locator=locator,
            reference=reference,
        )
        responses[_detail_url(value, "/document")] = _documents_body()
        responses[_detail_url(value, "/responses")] = _responses_body()
    return responses


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_opdc.py"
    name = "_test_qualify_opdc"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
        iterator = cast(
            "AsyncGenerator[DiscoveryBatch[OpdcCheckpointV1]]",
            OpdcAdapter().discover(
                _OpdcSession({registered: _search((1, "26/0001/FULOPDC"))}),
                WINDOW,
                None,
            ),
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


def test_opdc_live_detail_retains_metadata_comments_and_three_evidence_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            url=HttpUrl(document_url),
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

    monkeypatch.setattr(
        opdc_adapter,
        "bng_to_wgs84",
        lambda easting, northing: ApplicationLocation(
            bng_easting=easting,
            bng_northing=northing,
            wgs84=Wgs84Coordinate(longitude=-0.25, latitude=51.52),
        ),
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
            _detail_url("10008"): _updated_detail(
                fullProposal=" ",
                oneAppReference="",
                applicationType=None,
                decisionText=None,
                receivedDate=None,
                registrationDate=None,
                validDate=None,
                decisionDate=None,
                ward=None,
                easting=None,
                northing=None,
            ),
            _detail_url("10008", "/document"): b"[]",
            _detail_url("10008", "/responses"): b"[]",
        }
    )
    snapshot = asyncio.run(OpdcAdapter().fetch(session, reference))

    assert snapshot.payload.documents == ()
    assert snapshot.payload.responses == ()
    assert snapshot.payload.development_description == "Build homes"
    assert snapshot.completeness.documents.kind == "empty"
    assert snapshot.completeness.comments.kind == "empty"
    assert len(snapshot.evidence) == _DETAIL_EVIDENCE_COUNT


@pytest.mark.parametrize(
    ("documents", "responses"),
    [
        (SourceUnavailableError("documents unavailable"), b"not-json"),
        (_documents_body(duplicate=True), _responses_body(duplicate=True)),
        (_documents_body(invalid_date=True), b"[]"),
        (b"not-json", SourceUnavailableError("responses unavailable")),
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

    locator_mismatch = _OpdcSession({_detail_url("10008"): _detail_body(locator=10009)})
    with pytest.raises(ValueError, match="locator mismatch"):
        asyncio.run(adapter.fetch(locator_mismatch, reference))

    malformed = _OpdcSession({_detail_url("10008"): b"{}"})
    with pytest.raises(OpdcParseError, match="detail JSON"):
        asyncio.run(adapter.fetch(malformed, reference))

    missing_proposal = _OpdcSession(
        {
            _detail_url("10008"): _updated_detail(fullProposal="", proposal=""),
            _detail_url("10008", "/document"): b"[]",
            _detail_url("10008", "/responses"): b"[]",
        }
    )
    with pytest.raises(OpdcParseError, match="detail proposal"):
        asyncio.run(adapter.fetch(missing_proposal, reference))


def test_opdc_qualification_requires_safe_exact_scope_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live consent, older-open scope, valid dates, and safe reuse are explicit."""
    module = _qualification_module()
    created = 0

    def session_factory() -> _OpdcSession:
        nonlocal created
        created += 1
        return _OpdcSession(_qualification_responses())

    base = [
        "--data-dir",
        str(tmp_path / "missing-confirmation"),
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(base, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "confirmation-required"

    without_open = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "missing-open"),
        "--end",
        "2026-09-16",
    ]
    assert module.main(without_open, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "include-open-required"

    invalid = [
        "--confirm-live",
        "--data-dir",
        str(tmp_path / "invalid-date"),
        "--end",
        "not-a-date",
        "--include-open",
    ]
    assert module.main(invalid, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "invalid-date"

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("keep", encoding="utf-8")
    occupied_args = [
        "--confirm-live",
        "--data-dir",
        str(occupied),
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(occupied_args, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert (occupied / "preserve").read_text(encoding="utf-8") == "keep"
    assert created == 0


def test_opdc_qualification_persists_typed_proof_and_zero_network_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A complete persisted bootstrap emits a versioned success-only receipt."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    sessions: list[_OpdcSession] = []

    def session_factory() -> _OpdcSession:
        session = _OpdcSession(_qualification_responses())
        sessions.append(session)
        return session

    args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert (
        module.main(
            args,
            session_factory=session_factory,
            now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
        )
        == 0
    )

    assert len(sessions) == _QUALIFICATION_SESSION_COUNT
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == _QUALIFICATION_REQUEST_COUNT
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "opdc"
    assert receipt["source_contract"] == "agile-citizen-portal-v1"
    assert receipt["created_at"] == "2026-09-16T12:00:00Z"
    assert receipt["scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert receipt["query_inventory"] == [
        {"query": "registered-window", "result_total": 2},
        {"query": "determined-window", "result_total": 2},
        {"query": "registered-open", "result_total": 2},
    ]
    assert len(receipt["identities"]) == _QUALIFICATION_APPLICATION_COUNT
    assert receipt["counts"]["applications"] == _QUALIFICATION_APPLICATION_COUNT
    assert (
        receipt["counts"]["discovered_references"] == _QUALIFICATION_APPLICATION_COUNT
    )
    assert receipt["counts"]["pending_retries"] == 0
    assert receipt["counts"]["failed_sections"] == 0
    assert receipt["counts"]["unmapped_records"] == 0
    assert receipt["costs"]["initial"]["request_count"] == _QUALIFICATION_REQUEST_COUNT
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert all(check["ok"] for check in receipt["checks"])
    receipt_path = data_dir / "opdc-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt

    sessions.clear()
    assert module.main([*args, "--resume"], session_factory=session_factory) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert len(sessions) == _QUALIFICATION_SESSION_COUNT
    assert all(session.closed for session in sessions)
    assert all(session.requested_urls == () for session in sessions)
    assert resumed["costs"]["initial"]["request_count"] == 0
    assert resumed["costs"]["rerun"]["request_count"] == 0


def test_opdc_qualification_rejects_failed_sections_without_a_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A child-section failure remains visible and blocks qualification."""
    module = _qualification_module()
    data_dir = tmp_path / "failed"
    sessions: list[_OpdcSession] = []

    def session_factory() -> _OpdcSession:
        responses = _qualification_responses()
        responses[_detail_url("1", "/document")] = SourceUnavailableError(
            "documents unavailable"
        )
        session = _OpdcSession(responses)
        sessions.append(session)
        return session

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--end",
            "2026-09-16",
            "--include-open",
        ],
        session_factory=session_factory,
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert "application-evidence" in error["failed_checks"]
    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert not (data_dir / "opdc-qualification-v1.json").exists()


def test_opdc_qualification_refuses_a_changed_scope_before_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-empty qualification store cannot accumulate another date scope."""
    module = _qualification_module()
    data_dir = tmp_path / "scope"
    sessions: list[_OpdcSession] = []

    def session_factory() -> _OpdcSession:
        session = _OpdcSession(_qualification_responses())
        sessions.append(session)
        return session

    original = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert module.main(original, session_factory=session_factory) == 0
    capsys.readouterr()
    sessions.clear()

    changed = original.copy()
    changed[4] = "2026-09-17"
    changed.append("--resume")
    assert module.main(changed, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "scope-mismatch"
    assert sessions == []
