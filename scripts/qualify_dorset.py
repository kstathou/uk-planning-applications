# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist a bounded, evidence-backed Dorset live qualification receipt."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

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
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalRequest, PortalSession

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
        store.discovery_state(_AUTHORITY_ID).queued
    )
    application_references = _canonical_references(
        store.application_references(_AUTHORITY_ID)
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
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    checkpoint = _terminal_checkpoint(store, config.scope)
    proof = _evidence_proof(store)
    agreement = _reference_agreement(store, checkpoint)
    initial_checks = _base_checks(store, first_snapshot, initial, proof, agreement)
    _require(initial_checks)

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    final_checkpoint = _terminal_checkpoint(store, config.scope)
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


def _default_session() -> HttpxPortalSession:
    return HttpxPortalSession(max_attempts=1)


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
                receipt = asyncio.run(_qualify(store, config, session_factory, now))
                _write_receipt(config.data_dir / _RECEIPT_NAME, receipt)
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
