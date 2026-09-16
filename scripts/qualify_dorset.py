# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist a bounded, evidence-backed Dorset live qualification receipt."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast
from urllib.parse import urljoin

import httpx
from pydantic import Field

from yimby.authorities.dorset import DORSET_PACKAGE
from yimby.authorities.dorset.adapter import DorsetCheckpointV1, DorsetDiscoveryScope
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    DurableDiscoveryBatch,
    EvidenceCapture,
    FrozenModel,
    QualificationSnapshot,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceReference,
    StoredCheckpoint,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalRequest,
    PortalSession,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

_AUTHORITY_ID = AuthorityId("dorset")
_RECEIPT_NAME = "dorset-qualification-v1.json"
_DEFAULT_DATA_DIR = Path(".yimby/qualification-dorset-2026-09-16")
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_QUERY_INVENTORY = ("received-valid", "outstanding")
_SCOPE_START = date(2026, 8, 18)
_SCOPE_END = date(2026, 9, 16)
_HTTP_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "user-agent": "yimby/0.1 (+local planning research; contact via source repository)",
}
_OFFICIAL_HOST = "planning.dorsetcouncil.gov.uk"
_OFFICIAL_PATHS = frozenset(
    {
        "/advsearch.aspx",
        "/disclaimer.aspx",
        "/plandisp.aspx",
        "/searchresults.aspx",
    }
)
_ATTACHMENT_MEDIA_TYPES = frozenset(
    {
        "application/msword",
        "application/octet-stream",
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    }
)
_ATTACHMENT_MEDIA_PREFIXES = ("audio/", "image/", "video/")
_REDIRECT_DESTINATION_ERROR = "Dorset redirect destination outside official register"
_REQUEST_BOUNDARY_ERROR = "Dorset request outside official register boundary"


SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class DorsetQualificationScope(FrozenModel):
    """The fixed inclusive live-discovery period Dorset qualification proves."""

    start: date = _SCOPE_START
    end: date = _SCOPE_END
    include_open: Literal[True] = True


class DorsetQualificationCounts(FrozenModel):
    """Durable Dorset counts after a collection pass."""

    applications: int = Field(ge=0)
    discovered_references: int = Field(ge=0)
    native_versions: int = Field(ge=0)
    application_versions: int = Field(ge=0)
    document_versions: int = Field(ge=0)
    comment_versions: int = Field(ge=0)
    pending_retries: int = Field(ge=0)
    failed_sections: int = Field(ge=0)
    unmapped_records: int = Field(ge=0)


class DorsetQualificationCost(FrozenModel):
    """Transport work observed through the local qualification wrapper."""

    fetch_calls: int = Field(ge=0)
    successful_requests: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class DorsetQualificationCosts(FrozenModel):
    """Initial collection and immediate terminal rerun transport costs."""

    initial: DorsetQualificationCost
    rerun: DorsetQualificationCost


class DorsetReferenceAgreementV1(FrozenModel):
    """Canonical reference-set hashes from every durable representation."""

    count: int = Field(gt=0)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    durable_queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    applications_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DorsetEvidenceProofV1(FrozenModel):
    """Proof that each retained unique capture decompresses and hashes exactly."""

    records: int = Field(ge=0)
    unique_digests: int = Field(ge=0)
    decompressed: int = Field(ge=0)
    digest_matches: int = Field(ge=0)
    failed_digests: tuple[str, ...] = ()


class DorsetQualificationCheck(FrozenModel):
    """One named qualification invariant."""

    name: str
    ok: bool


class DorsetQualificationCycleV1(FrozenModel):
    """A scheduled post-qualification review cycle."""

    sequence: int = Field(ge=1)
    due_on: date
    status: Literal["pending"] = "pending"


class DorsetQualificationReceiptV1(FrozenModel):
    """Typed receipt for the dated Dorset discovery-only qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["dorset"] = "dorset"
    created_at: datetime
    scope: DorsetQualificationScope
    query_inventory: tuple[str, str]
    source_run_id: str = Field(min_length=1)
    terminal_checkpoint: DorsetCheckpointV1
    reference_agreement: DorsetReferenceAgreementV1
    counts: DorsetQualificationCounts
    costs: DorsetQualificationCosts
    evidence: DorsetEvidenceProofV1
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[DorsetQualificationCheck, ...]
    weekly_cycles: tuple[DorsetQualificationCycleV1, ...]
    readiness: Literal["discovery-only"] = "discovery-only"
    http_max_attempts: Literal[1] = 1


class _Config(FrozenModel):
    data_dir: Path
    scope: DorsetQualificationScope
    restart_discovery: bool


class QualificationConfigError(ValueError):
    """A mandatory live-qualification option was missing or unsafe."""


class QualificationFailedError(RuntimeError):
    """A persisted qualification invariant was not met."""

    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        """Keep stable check names for the command's machine-readable failure."""
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


class _DorsetRateLimitedStream(httpx.AsyncByteStream):
    """Hold Dorset's host turn until one response body is closed."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        turn: AbstractAsyncContextManager[None],
    ) -> None:
        self._stream = stream
        self._turn = turn
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        finally:
            await self._turn.__aexit__(None, None, None)


class _DorsetRateLimitedTransport(httpx.AsyncBaseTransport):
    """Apply Dorset's host gap to each network hop, including redirects."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        limiter: HostRateLimiter,
    ) -> None:
        self._transport = transport
        self._limiter = limiter
        self._attachment_body_requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_official_dorset_url(request.url):
            raise SourceUnavailableError(_REQUEST_BOUNDARY_ERROR)
        turn = self._limiter.turn(request.url.host)
        await turn.__aenter__()
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException as error:
            await turn.__aexit__(type(error), error, error.__traceback__)
            raise
        if _is_attachment_response(response):
            self._attachment_body_requests += 1
            blocked_error = AttachmentBodyBlockedError(
                f"attachment body blocked for {_OFFICIAL_HOST}"
            )
            await _close_rejected_response(response, turn, blocked_error)
        location = response.headers.get("location")
        if response.is_redirect and location is not None:
            destination = httpx.URL(urljoin(str(request.url), location))
            if not _is_official_dorset_url(destination):
                redirect_error = SourceUnavailableError(_REDIRECT_DESTINATION_ERROR)
                await _close_rejected_response(response, turn, redirect_error)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_DorsetRateLimitedStream(
                cast("httpx.AsyncByteStream", response.stream), turn
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def attachment_body_requests(self) -> int:
        """Return response bodies rejected at any Dorset network hop."""
        return self._attachment_body_requests


class _DorsetPortalSession(HttpxPortalSession):
    """Expose authority-local blocks alongside shared session accounting."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        transport: _DorsetRateLimitedTransport,
    ) -> None:
        self._dorset_transport = transport
        super().__init__(
            client=client,
            limiter=HostRateLimiter(0),
            max_attempts=1,
        )

    @property
    def attachment_body_requests(self) -> int:
        """Count final-response and redirect-hop attachment blocks."""
        return (
            super().attachment_body_requests
            + self._dorset_transport.attachment_body_requests
        )


def _is_official_dorset_url(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == _OFFICIAL_HOST
        and url.port in (None, 443)
        and not url.userinfo
        and url.path in _OFFICIAL_PATHS
    )


def _is_attachment_response(response: httpx.Response) -> bool:
    disposition = response.headers.get("content-disposition", "").casefold()
    media_type = (
        response.headers.get("content-type", "").partition(";")[0].strip().casefold()
    )
    return (
        "attachment" in disposition
        or "filename" in disposition
        or media_type in _ATTACHMENT_MEDIA_TYPES
        or media_type.startswith(_ATTACHMENT_MEDIA_PREFIXES)
    )


async def _close_rejected_response(
    response: httpx.Response,
    turn: AbstractAsyncContextManager[None],
    error: Exception,
) -> NoReturn:
    try:
        await response.aclose()
    finally:
        await turn.__aexit__(type(error), error, error.__traceback__)
    raise error


class _MeasuringSession:
    """Preserve PortalSession behaviour while counting attempted fetches exactly."""

    def __init__(self, session: PortalSession) -> None:
        self._session = session
        self.fetch_calls = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        """Count before delegation, including a request that raises at transport."""
        self.fetch_calls += 1
        return await self._session.fetch(request)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        """Expose the underlying successful request log."""
        return self._session.requested_urls

    @property
    def attachment_body_requests(self) -> int:
        """Expose blocked attachment retrieval attempts."""
        return self._session.attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        """Expose body bytes transferred by the wrapped session."""
        return self._session.transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        """Expose browser time for the collector's persisted metrics."""
        return self._session.browser_time_ms

    @property
    def mode(self) -> TransportMode:
        """Preserve fixture or live dispatch for the Dorset adapter."""
        return self._session.mode

    async def aclose(self) -> None:
        """Close the wrapped transport once its collection pass ends."""
        await self._session.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify the dated Dorset live collection.",
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--include-open", action="store_true")
    parser.add_argument("--data-dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--restart-discovery", action="store_true")
    return parser


def _config(argv: Sequence[str]) -> _Config:
    arguments = _parser().parse_args(argv)
    if not arguments.confirm_live:
        raise QualificationConfigError(_CONFIRMATION_REQUIRED)
    if not arguments.include_open:
        raise QualificationConfigError(_INCLUDE_OPEN_REQUIRED)
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(
        data_dir=data_dir,
        scope=DorsetQualificationScope(),
        restart_discovery=arguments.restart_discovery,
    )


async def _collect_once(
    collector: Collector,
    window: DiscoveryWindow,
    session_factory: SessionFactory,
) -> DorsetQualificationCost:
    session = _MeasuringSession(session_factory())
    try:
        report = await collector.collect(_AUTHORITY_ID, window, session)
        return DorsetQualificationCost(
            fetch_calls=session.fetch_calls,
            successful_requests=len(session.requested_urls),
            transferred_bytes=session.transferred_bytes,
            attachment_body_requests=report.attachment_body_requests,
        )
    finally:
        await session.aclose()


def _terminal_checkpoint(
    store: SqliteStore,
    scope: DorsetQualificationScope,
) -> DorsetCheckpointV1:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        raise QualificationFailedError(("terminal-checkpoint",))
    try:
        checkpoint = DorsetCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError as error:
        raise QualificationFailedError(("terminal-checkpoint",)) from error
    expected_scope = DorsetDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )
    if (
        checkpoint.object_offset != "live"
        or checkpoint.live_scope != expected_scope
        or not checkpoint.live_complete
        or checkpoint.completed_queries != _QUERY_INVENTORY
        or checkpoint.active_query is not None
        or checkpoint.next_page != 1
        or checkpoint.total_pages is not None
        or checkpoint.active_references
        or checkpoint.active_new_references
    ):
        raise QualificationFailedError(("terminal-checkpoint",))
    return checkpoint


def _canonical_references(
    references: Sequence[SourceReference],
) -> tuple[SourceReference, ...]:
    return tuple(
        sorted(
            references,
            key=lambda item: (str(item.source_id), item.reference, item.locator or ""),
        )
    )


def _reference_hash(references: Sequence[SourceReference]) -> str:
    payload = [reference.model_dump(mode="json") for reference in references]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return sha256(encoded).hexdigest()


def _reference_agreement(
    store: SqliteStore,
    checkpoint: DorsetCheckpointV1,
) -> DorsetReferenceAgreementV1:
    checkpoint_references = _canonical_references(checkpoint.seen_references)
    durable_references = _canonical_references(
        _current_inventory(
            store.discovery_state(_AUTHORITY_ID).queued,
            checkpoint_references,
        )
    )
    application_references = _canonical_references(
        _current_inventory(
            store.application_references(_AUTHORITY_ID),
            checkpoint_references,
        )
    )
    if (
        not checkpoint_references
        or len(set(checkpoint_references)) != len(checkpoint_references)
        or checkpoint_references != durable_references
        or checkpoint_references != application_references
    ):
        raise QualificationFailedError(("reference-agreement",))
    return DorsetReferenceAgreementV1(
        count=len(checkpoint_references),
        checkpoint_sha256=_reference_hash(checkpoint_references),
        durable_queue_sha256=_reference_hash(durable_references),
        applications_sha256=_reference_hash(application_references),
    )


def _current_inventory(
    durable: Sequence[SourceReference],
    expected: Sequence[SourceReference],
) -> tuple[SourceReference, ...]:
    expected_identities = {
        (reference.source_id, reference.reference, reference.locator)
        for reference in expected
    }
    return tuple(
        reference
        for reference in durable
        if (reference.source_id, reference.reference, reference.locator)
        in expected_identities
    )


def _evidence_proof(
    store: SqliteStore,
) -> DorsetEvidenceProofV1:
    retained = store.retained_native_records()
    captures: dict[str, bytes] = {}
    failed: set[str] = set()
    for record in retained:
        for capture in record.evidence:
            digest = str(capture.digest)
            body = capture.body
            if sha256(body).hexdigest() != digest:
                failed.add(digest)
            previous = captures.get(digest)
            if previous is not None and previous != body:
                failed.add(digest)
            captures[digest] = body
    proof = DorsetEvidenceProofV1(
        records=len(retained),
        unique_digests=len(captures),
        decompressed=len(captures),
        digest_matches=len(captures) - len(failed),
        failed_digests=tuple(sorted(failed)),
    )
    if failed:
        raise QualificationFailedError(("evidence-integrity",))
    return proof


def _counts(snapshot: QualificationSnapshot) -> DorsetQualificationCounts:
    return DorsetQualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _base_checks(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
    initial: DorsetQualificationCost,
    proof: DorsetEvidenceProofV1,
    agreement: DorsetReferenceAgreementV1,
) -> tuple[DorsetQualificationCheck, ...]:
    return (
        DorsetQualificationCheck(name="terminal-checkpoint", ok=True),
        DorsetQualificationCheck(
            name="live-source-cost",
            ok=(
                initial.fetch_calls > 0
                and initial.fetch_calls == initial.successful_requests
                and initial.transferred_bytes > 0
            ),
        ),
        DorsetQualificationCheck(
            name="reference-agreement",
            ok=(
                agreement.count > 0
                and len(
                    {
                        agreement.checkpoint_sha256,
                        agreement.durable_queue_sha256,
                        agreement.applications_sha256,
                    }
                )
                == 1
            ),
        ),
        DorsetQualificationCheck(
            name="pending-retries",
            ok=snapshot.pending_retries == 0,
        ),
        DorsetQualificationCheck(
            name="failed-sections",
            ok=snapshot.failed_sections == 0,
        ),
        DorsetQualificationCheck(
            name="attachment-policy",
            ok=initial.attachment_body_requests == 0,
        ),
        DorsetQualificationCheck(
            name="database-integrity",
            ok=store.database_integrity() == "ok",
        ),
        DorsetQualificationCheck(
            name="evidence-integrity",
            ok=(
                proof.records > 0
                and proof.unique_digests > 0
                and proof.unique_digests == proof.decompressed == proof.digest_matches
                and not proof.failed_digests
                and not store.missing_evidence_paths()
            ),
        ),
        DorsetQualificationCheck(
            name="application-count",
            ok=(
                snapshot.applications > 0
                and snapshot.applications == snapshot.discovered_references
                and snapshot.applications == agreement.count
            ),
        ),
        DorsetQualificationCheck(
            name="unmapped-records",
            ok=snapshot.unmapped_records == 0,
        ),
    )


def _require(checks: Sequence[DorsetQualificationCheck]) -> None:
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise QualificationFailedError(failed)


def _restart_discovery_checkpoint(
    store: SqliteStore,
    scope: DorsetQualificationScope,
) -> None:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        raise QualificationFailedError(("restart-checkpoint",))
    try:
        current = DorsetCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError as error:
        raise QualificationFailedError(("restart-checkpoint",)) from error
    expected_scope = DorsetDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )
    if current.live_scope != expected_scope:
        raise QualificationFailedError(("restart-checkpoint",))
    if (
        current.completed_queries == ("received-valid",)
        and current.active_query in {None, "outstanding"}
        and not current.live_complete
    ):
        active_new = {
            (reference.source_id, reference.reference, reference.locator)
            for reference in current.active_new_references
        }
        seen = {
            (reference.source_id, reference.reference, reference.locator)
            for reference in current.seen_references
        }
        if (
            len(active_new) != len(current.active_new_references)
            or len(seen) != len(current.seen_references)
            or not active_new.issubset(seen)
        ):
            raise QualificationFailedError(("restart-checkpoint",))
        reset = current.model_copy(
            update={
                "active_query": None,
                "next_page": 1,
                "total_pages": None,
                "active_references": (),
                "active_new_references": (),
                "seen_references": tuple(
                    reference
                    for reference in current.seen_references
                    if (
                        reference.source_id,
                        reference.reference,
                        reference.locator,
                    )
                    not in active_new
                ),
            }
        )
    else:
        reset = DorsetCheckpointV1(object_offset="live", live_scope=expected_scope)
    run_id = store.begin_run(_AUTHORITY_ID)
    store.commit_discovery(
        run_id,
        _AUTHORITY_ID,
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=StoredCheckpoint(
                schema_version=1,
                payload_json=reset.model_dump_json(),
            ),
            complete=False,
        ),
    )
    store.finish_run(
        run_id,
        _AUTHORITY_ID,
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=0,
                transferred_bytes=0,
                duration_ms=0,
                storage_growth_bytes=0,
            ),
            transport_mode=TransportMode.NOT_RUN,
        ),
    )


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
    prior_receipt: DorsetQualificationReceiptV1 | None,
) -> DorsetQualificationReceiptV1:
    registry = AuthorityRegistry((DORSET_PACKAGE,))
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    if config.restart_discovery:
        _restart_discovery_checkpoint(store, config.scope)
    prior_status_count = len(store.run_statuses())
    observed_initial = await _collect_once(collector, window, session_factory)
    source_run_id, source_metrics = store.latest_successful_nonempty_run(_AUTHORITY_ID)
    initial = observed_initial
    if observed_initial.fetch_calls == 0:
        if prior_receipt is None:
            raise QualificationFailedError(("live-source-proof",))
        prior_cost = prior_receipt.costs.initial
        if (
            prior_receipt.source_run_id != source_run_id
            or prior_cost.fetch_calls != source_metrics.request_count
            or prior_cost.successful_requests != source_metrics.request_count
            or prior_cost.transferred_bytes != source_metrics.transferred_bytes
            or prior_cost.attachment_body_requests != 0
        ):
            raise QualificationFailedError(("live-source-proof",))
        initial = prior_cost
    checkpoint = _terminal_checkpoint(store, config.scope)
    first_snapshot = store.qualification_snapshot(
        _AUTHORITY_ID,
        inventory=checkpoint.seen_references,
    )
    proof = _evidence_proof(store)
    agreement = _reference_agreement(store, checkpoint)
    initial_checks = _base_checks(store, first_snapshot, initial, proof, agreement)
    _require(initial_checks)

    rerun = await _collect_once(collector, window, session_factory)
    final_checkpoint = _terminal_checkpoint(store, config.scope)
    final_snapshot = store.qualification_snapshot(
        _AUTHORITY_ID,
        inventory=final_checkpoint.seen_references,
    )
    final_proof = _evidence_proof(store)
    final_agreement = _reference_agreement(store, final_checkpoint)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(store, final_snapshot, initial, final_proof, final_agreement),
        DorsetQualificationCheck(
            name="idempotent-rerun",
            ok=(
                first_snapshot == final_snapshot
                and checkpoint == final_checkpoint
                and agreement == final_agreement
                and proof == final_proof
            ),
        ),
        DorsetQualificationCheck(
            name="terminal-rerun-costs",
            ok=(
                rerun.fetch_calls == 0
                and rerun.successful_requests == 0
                and rerun.transferred_bytes == 0
                and rerun.attachment_body_requests == 0
            ),
        ),
        DorsetQualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    return DorsetQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        query_inventory=_QUERY_INVENTORY,
        source_run_id=source_run_id,
        terminal_checkpoint=final_checkpoint,
        reference_agreement=final_agreement,
        counts=_counts(final_snapshot),
        costs=DorsetQualificationCosts(initial=initial, rerun=rerun),
        evidence=final_proof,
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_cycles=(
            DorsetQualificationCycleV1(sequence=1, due_on=date(2026, 9, 23)),
            DorsetQualificationCycleV1(sequence=2, due_on=date(2026, 9, 30)),
        ),
    )


def _write_receipt(path: Path, receipt: DorsetQualificationReceiptV1) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    payload = f"{receipt.model_dump_json(indent=2)}\n"
    with temporary.open("w", encoding="utf-8") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _existing_receipt(path: Path) -> DorsetQualificationReceiptV1 | None:
    if not path.is_file():
        return None
    return DorsetQualificationReceiptV1.model_validate_json(path.read_text())


def _default_session(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter: HostRateLimiter | None = None,
) -> HttpxPortalSession:
    network_limiter = limiter or HostRateLimiter()
    dorset_transport = _DorsetRateLimitedTransport(
        transport or httpx.AsyncHTTPTransport(),
        network_limiter,
    )
    return _DorsetPortalSession(
        client=httpx.AsyncClient(
            follow_redirects=True,
            headers=_HTTP_HEADERS,
            timeout=httpx.Timeout(30.0),
            transport=dorset_transport,
        ),
        transport=dorset_transport,
    )


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _error(code: str, exit_code: int, **details: object) -> int:
    print(json.dumps({"error": code, **details}, sort_keys=True), file=sys.stderr)
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory = _default_session,
    now: Clock = _default_clock,
) -> int:
    """Run the explicit fixed-scope Dorset live qualification."""
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except QualificationConfigError as error:
        return _error(str(error), 2)
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            store = SqliteStore(
                config.data_dir / "yimby.sqlite3",
                EvidenceStore(config.data_dir / "evidence"),
            )
            try:
                receipt_path = config.data_dir / _RECEIPT_NAME
                receipt = asyncio.run(
                    _qualify(
                        store,
                        config,
                        session_factory,
                        now,
                        _existing_receipt(receipt_path),
                    )
                )
                _write_receipt(receipt_path, receipt)
            finally:
                store.close()
    except QualificationFailedError as error:
        return _error(
            "qualification-failed",
            1,
            failed_checks=list(error.failed_checks),
        )
    except Exception as error:  # noqa: BLE001
        return _error("runtime-failure", 1, exception=type(error).__name__)
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
