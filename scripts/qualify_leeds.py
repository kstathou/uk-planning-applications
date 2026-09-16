# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Leeds live collection without changing registry readiness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from yimby.authorities.leeds import LEEDS_PACKAGE
from yimby.authorities.leeds.adapter import (
    SOURCE,
    LeedsCheckpointV1,
    LeedsDetailUnavailableError,
    LeedsDetailUnverifiedError,
    LeedsDiscoveryScope,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
    SourceReference,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession, SourceUnavailableError

_AUTHORITY_ID = AuthorityId("leeds")
_RECEIPT_NAME = "leeds-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_EXACT_WINDOW_REQUIRED = "30-day-window-required"
_WINDOW_SPAN_DAYS = 29
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_MAX_REFERENCE_FAILURES = 3
_MAX_NO_PROGRESS_FAILURES = 3
_WEEKLY_DATE_TYPES = ("DC_Validated", "DC_Decided")
_CASE_TYPE_VALUES = (
    "DAG",
    "ADV",
    "CLA",
    "CLE",
    "CLP",
    "DEM",
    "COND",
    "EXT",
    "EDD",
    "BNG106",
    "FU",
    "HAZ",
    "DHH",
    "LI",
    "LA",
    "LATR",
    "S106",
    "MOD",
    "N1490",
    "NPD",
    "OT",
    "DPD",
    "PIP",
    "PRESME",
    "RM",
    "TDC",
    "DTM",
    "TWA",
    "TR",
    "UNK",
)

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]
Identity = tuple[str, str, str]


class QualificationScope(FrozenModel):
    """Exact inclusive discovery scope proven by the receipt."""

    start: date
    end: date
    include_open: Literal[True] = True


class QualificationCounts(FrozenModel):
    """Durable Leeds counts after collection."""

    applications: int = Field(ge=0)
    discovered_references: int = Field(ge=0)
    native_versions: int = Field(ge=0)
    application_versions: int = Field(ge=0)
    document_versions: int = Field(ge=0)
    comment_versions: int = Field(ge=0)
    pending_retries: int = Field(ge=0)
    failed_sections: int = Field(ge=0)
    unmapped_records: int = Field(ge=0)


class QualificationCost(FrozenModel):
    """Observable transport cost for one qualification pass."""

    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    """First collection and immediate idempotence proof costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class DurableSetAgreementV1(FrozenModel):
    """Exact agreement of three independently read durable identity sets."""

    algorithm: Literal["sha256-canonical-json-v1"] = "sha256-canonical-json-v1"
    checkpoint_count: int = Field(ge=1)
    queue_count: int = Field(ge=1)
    application_count: int = Field(ge=1)
    checkpoint_digest: str
    queue_digest: str
    application_digest: str
    exact: Literal[True] = True


class EvidenceIntegrityV1(FrozenModel):
    """Rehydrated current application evidence proof."""

    checked_capture_count: int = Field(ge=1)
    missing_path_count: Literal[0] = 0
    digest_mismatch_count: Literal[0] = 0


class PendingWeeklyCycleV1(FrozenModel):
    """One future weekly refresh that cannot be proven on bootstrap day."""

    ordinal: Literal[1, 2]
    status: Literal["pending"] = "pending"
    due_on: date
    reason: Literal[
        "a genuinely later live refresh cannot be completed on the bootstrap date"
    ] = "a genuinely later live refresh cannot be completed on the bootstrap date"


class LeedsQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local Leeds bootstrap qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["leeds"] = "leeds"
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[str, ...]
    query_totals: tuple[int, ...]
    counts: QualificationCounts
    costs: QualificationCosts
    durable_sets: DurableSetAgreementV1
    evidence_integrity: EvidenceIntegrityV1
    run_statuses: tuple[RunStatus, ...]
    weekly_cycles: tuple[PendingWeeklyCycleV1, PendingWeeklyCycleV1]
    live_ready_promoted: Literal[False] = False
    checks: tuple[QualificationCheck, ...]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope


class _QualificationProofs(FrozenModel):
    checkpoint: LeedsCheckpointV1 | None
    agreement: DurableSetAgreementV1 | None
    evidence: EvidenceIntegrityV1 | None


class QualificationConfigError(ValueError):
    """One required safety option or scope value is invalid."""

    def __init__(self, code: str) -> None:
        """Retain the stable error code emitted by the command."""
        super().__init__(code)


class QualificationFailedError(RuntimeError):
    """Qualification invariants did not all hold."""

    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        """Retain the stable names of failed invariants."""
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify Leeds live collection.",
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--include-open", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def _config(argv: Sequence[str]) -> _Config:
    arguments = _parser().parse_args(argv)
    if not arguments.confirm_live:
        raise QualificationConfigError(_CONFIRMATION_REQUIRED)
    if not arguments.include_open:
        raise QualificationConfigError(_INCLUDE_OPEN_REQUIRED)
    try:
        start = date.fromisoformat(arguments.start)
        end = date.fromisoformat(arguments.end)
    except ValueError as error:
        raise QualificationConfigError(_INVALID_DATE) from error
    if start > end:
        raise QualificationConfigError(_INVALID_WINDOW)
    if (end - start).days != _WINDOW_SPAN_DAYS:
        raise QualificationConfigError(_EXACT_WINDOW_REQUIRED)
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(
        data_dir=data_dir,
        scope=QualificationScope(start=start, end=end),
    )


async def _collect_once(
    collector: Collector,
    window: DiscoveryWindow,
    session_factory: SessionFactory,
) -> QualificationCost:
    session = session_factory()
    try:
        report = await collector.collect(_AUTHORITY_ID, window, session)
        return QualificationCost(
            request_count=len(report.requested_urls),
            transferred_bytes=session.transferred_bytes,
            attachment_body_requests=report.attachment_body_requests,
        )
    finally:
        await session.aclose()


def _session_cost(session: PortalSession) -> QualificationCost:
    return QualificationCost(
        request_count=len(session.requested_urls),
        transferred_bytes=session.transferred_bytes,
        attachment_body_requests=session.attachment_body_requests,
    )


def _add_cost(left: QualificationCost, right: QualificationCost) -> QualificationCost:
    return QualificationCost(
        request_count=left.request_count + right.request_count,
        transferred_bytes=left.transferred_bytes + right.transferred_bytes,
        attachment_body_requests=(
            left.attachment_body_requests + right.attachment_body_requests
        ),
    )


def _progress_signature(store: SqliteStore) -> tuple[int, int, int]:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    pending_attempts = sum(
        item.attempts
        for item in store.retry_items()
        if item.authority_id == _AUTHORITY_ID and item.status == "pending"
    )
    return (
        snapshot.discovered_references,
        snapshot.applications,
        pending_attempts,
    )


def _reference_failure_limit_reached(store: SqliteStore) -> bool:
    return any(
        item.authority_id == _AUTHORITY_ID
        and item.status == "pending"
        and item.attempts >= _MAX_REFERENCE_FAILURES
        for item in store.retry_items()
    )


async def _collect_initial(
    collector: Collector,
    store: SqliteStore,
    window: DiscoveryWindow,
    session_factory: SessionFactory,
) -> QualificationCost:
    total = QualificationCost(
        request_count=0,
        transferred_bytes=0,
        attachment_body_requests=0,
    )
    progress = _progress_signature(store)
    no_progress_failures = 0
    while True:
        session = session_factory()
        try:
            await collector.collect(_AUTHORITY_ID, window, session)
        except (
            LeedsDetailUnavailableError,
            LeedsDetailUnverifiedError,
            SourceUnavailableError,
        ):
            total = _add_cost(total, _session_cost(session))
            current = _progress_signature(store)
            no_progress_failures = (
                no_progress_failures + 1 if current == progress else 0
            )
            progress = current
            if (
                _reference_failure_limit_reached(store)
                or no_progress_failures >= _MAX_NO_PROGRESS_FAILURES
            ):
                raise
        else:
            return _add_cost(total, _session_cost(session))
        finally:
            await session.aclose()


def _expected_query_inventory(scope: QualificationScope) -> tuple[str, ...]:
    monday = scope.start - timedelta(days=scope.start.weekday())
    weekly: list[str] = []
    while monday <= scope.end:
        if monday + timedelta(days=6) >= scope.start:
            weekly.extend(
                f"{monday.strftime('%d/%m/%Y')}|{date_type}"
                for date_type in _WEEKLY_DATE_TYPES
            )
        monday += timedelta(days=7)
    return (
        *weekly,
        f"advanced|validated|{scope.start.isoformat()}|{scope.end.isoformat()}",
        f"advanced|decision|{scope.start.isoformat()}|{scope.end.isoformat()}",
        *(f"advanced|current|{value}" for value in _CASE_TYPE_VALUES),
        "advanced|appeal|Appeal lodged",
    )


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> LeedsCheckpointV1 | None:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = LeedsCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None
    expected_scope = LeedsDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=True,
    )
    expected_queries = _expected_query_inventory(scope)
    identities = tuple(
        (str(SOURCE), item.reference, item.locator)
        for item in checkpoint.seen_identities
    )
    queue = _reference_identities(state.queued)
    valid = (
        checkpoint.result_page == "live"
        and checkpoint.live_scope == expected_scope
        and checkpoint.live_complete
        and checkpoint.completed_queries == expected_queries
        and len(checkpoint.query_totals) == len(expected_queries)
        and all(total >= 0 for total in checkpoint.query_totals)
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and bool(identities)
        and len(identities) == len(set(identities))
        and tuple(sorted(identities)) == queue
    )
    return checkpoint if valid else None


def _reference_identities(
    references: tuple[SourceReference, ...],
) -> tuple[Identity, ...]:
    if any(reference.locator is None for reference in references):
        return ()
    return tuple(
        sorted(
            (
                str(reference.source_id),
                reference.reference,
                str(reference.locator),
            )
            for reference in references
        )
    )


def _identity_digest(identities: tuple[Identity, ...]) -> str:
    payload = json.dumps(identities, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _set_agreement(
    store: SqliteStore,
    checkpoint: LeedsCheckpointV1,
) -> DurableSetAgreementV1 | None:
    checkpoint_identities = tuple(
        sorted(
            (str(SOURCE), identity.reference, identity.locator)
            for identity in checkpoint.seen_identities
        )
    )
    queue_identities = _reference_identities(
        store.discovery_state(_AUTHORITY_ID).queued
    )
    application_identities = _reference_identities(
        tuple(
            record.reference
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
        )
    )
    if not (
        checkpoint_identities
        and checkpoint_identities == queue_identities == application_identities
    ):
        return None
    digest = _identity_digest(checkpoint_identities)
    return DurableSetAgreementV1(
        checkpoint_count=len(checkpoint_identities),
        queue_count=len(queue_identities),
        application_count=len(application_identities),
        checkpoint_digest=digest,
        queue_digest=_identity_digest(queue_identities),
        application_digest=_identity_digest(application_identities),
    )


def _evidence_integrity(store: SqliteStore) -> EvidenceIntegrityV1 | None:
    records = tuple(
        record
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    )
    captures = tuple(capture for record in records for capture in record.evidence)
    mismatch_count = sum(
        hashlib.sha256(capture.body).hexdigest() != str(capture.digest)
        for capture in captures
    )
    missing_paths = store.missing_evidence_paths()
    if not captures or mismatch_count or missing_paths:
        return None
    return EvidenceIntegrityV1(checked_capture_count=len(captures))


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _check(name: str, *, ok: bool) -> QualificationCheck:
    return QualificationCheck(name=name, ok=ok)


def _base_checks(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
    proofs: _QualificationProofs,
    initial: QualificationCost,
) -> tuple[QualificationCheck, ...]:
    checkpoint = proofs.checkpoint
    return (
        _check("terminal-checkpoint-coherence", ok=checkpoint is not None),
        _check(
            "exact-query-inventory",
            ok=checkpoint is not None
            and len(checkpoint.completed_queries) == len(checkpoint.query_totals),
        ),
        _check(
            "durable-reference-application-agreement",
            ok=proofs.agreement is not None,
        ),
        _check("pending-retries-zero", ok=snapshot.pending_retries == 0),
        _check("failed-current-sections-zero", ok=snapshot.failed_sections == 0),
        _check("sqlite-integrity", ok=store.database_integrity() == "ok"),
        _check("evidence-integrity", ok=proofs.evidence is not None),
        _check("unmapped-records-zero", ok=snapshot.unmapped_records == 0),
        _check(
            "attachment-body-requests-zero",
            ok=initial.attachment_body_requests == 0,
        ),
        _check("application-count-nonzero", ok=snapshot.applications > 0),
        _check("live-ready-not-promoted", ok=True),
    )


def _require(checks: tuple[QualificationCheck, ...]) -> None:
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise QualificationFailedError(failed)


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> LeedsQualificationReceiptV1:
    collector = Collector(AuthorityRegistry((LEEDS_PACKAGE,)), store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    prior_status_count = len(store.run_statuses())
    initial = await _collect_initial(
        collector,
        store,
        window,
        session_factory,
    )
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    checkpoint = _terminal_checkpoint(store, config.scope)
    agreement = None if checkpoint is None else _set_agreement(store, checkpoint)
    evidence = _evidence_integrity(store)
    proofs = _QualificationProofs(
        checkpoint=checkpoint,
        agreement=agreement,
        evidence=evidence,
    )
    initial_checks = _base_checks(
        store,
        first_snapshot,
        proofs,
        initial,
    )
    _require(initial_checks)
    if checkpoint is None or agreement is None or evidence is None:
        raise QualificationFailedError(("initial-proof-state",))

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    final_checkpoint = _terminal_checkpoint(store, config.scope)
    final_agreement = (
        None if final_checkpoint is None else _set_agreement(store, final_checkpoint)
    )
    final_evidence = _evidence_integrity(store)
    final_proofs = _QualificationProofs(
        checkpoint=final_checkpoint,
        agreement=final_agreement,
        evidence=final_evidence,
    )
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(
            store,
            final_snapshot,
            final_proofs,
            initial,
        ),
        _check(
            "idempotent-rerun",
            ok=first_snapshot == final_snapshot
            and checkpoint == final_checkpoint
            and agreement == final_agreement
            and evidence == final_evidence,
        ),
        _check(
            "terminal-rerun-zero-network",
            ok=rerun.request_count == 0
            and rerun.transferred_bytes == 0
            and rerun.attachment_body_requests == 0,
        ),
        _check(
            "run-statuses-succeeded",
            ok=run_statuses[-2:]
            == (
                RunStatus.SUCCEEDED,
                RunStatus.SUCCEEDED,
            ),
        ),
        _check("weekly-cycles-pending", ok=True),
    )
    _require(final_checks)
    return LeedsQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        query_inventory=checkpoint.completed_queries,
        query_totals=checkpoint.query_totals,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        durable_sets=agreement,
        evidence_integrity=evidence,
        run_statuses=run_statuses,
        weekly_cycles=(
            PendingWeeklyCycleV1(
                ordinal=1,
                due_on=config.scope.end + timedelta(days=7),
            ),
            PendingWeeklyCycleV1(
                ordinal=2,
                due_on=config.scope.end + timedelta(days=14),
            ),
        ),
        checks=final_checks,
    )


def _write_receipt(path: Path, receipt: LeedsQualificationReceiptV1) -> None:
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
    return HttpxPortalSession(max_attempts=5)


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
    """Run explicit live qualification and emit its atomic receipt."""
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
