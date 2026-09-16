# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Arun live collection without changing registry readiness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from yimby.authorities.arun import ARUN_PACKAGE
from yimby.authorities.arun.adapter import (
    ArunApplicationV1,
    ArunCheckpointV1,
    ArunComplete,
    ArunDiscoveryScope,
    ArunLiveCursor,
    ArunQuery,
    _canonical_query_plan,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    FrozenModel,
    LiveReadiness,
    QualificationSnapshot,
    RunStatus,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import PILOT_LIVE_STATUS, AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("arun")
_RECEIPT_NAME = "arun-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_THIRTY_DAY_WINDOW_REQUIRED = "thirty-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_INCLUSIVE_WINDOW_SPAN_DAYS = 29
_EVIDENCE_PER_APPLICATION = 2

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationScope(FrozenModel):
    """Exact inclusive discovery scope proven by the receipt."""

    start: date
    end: date
    include_open: Literal[True] = True


class QualificationQuery(FrozenModel):
    """One canonical query and its reconciled result count."""

    query: ArunQuery
    reported_count: int = Field(ge=0, lt=200)
    enumerated_count: int = Field(ge=0, lt=200)


class QualificationReferences(FrozenModel):
    """Exact durable reference sets compared during qualification."""

    discovered: tuple[str, ...]
    retained_native: tuple[str, ...]
    applications: tuple[str, ...]


class QualificationEvidence(FrozenModel):
    """Recomputed evidence inventory retained for the qualified records."""

    capture_count: int = Field(ge=0)
    digests: tuple[str, ...]


class QualificationCounts(FrozenModel):
    """Durable authority counts after collection."""

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
    """First collection and immediate terminal-rerun costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class WeeklyCycle(FrozenModel):
    """A later operational qualification cycle not yet run."""

    target_date: date
    status: Literal["pending"] = "pending"


class ArunQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local Arun bootstrap qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["arun"] = "arun"
    created_at: datetime
    scope: QualificationScope
    bootstrap_status: Literal["proved"] = "proved"
    operational_status: Literal["pending-weekly-refreshes"] = "pending-weekly-refreshes"
    registry_readiness: Literal["discovery-only"] = "discovery-only"
    query_inventory: tuple[QualificationQuery, ...]
    references: QualificationReferences
    evidence: QualificationEvidence
    counts: QualificationCounts
    semantic_fingerprint: str
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    weekly_cycles: tuple[WeeklyCycle, ...]
    checks: tuple[QualificationCheck, ...]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope


class _QualificationState(FrozenModel):
    snapshot: QualificationSnapshot
    query_inventory: tuple[QualificationQuery, ...]
    references: QualificationReferences
    evidence: QualificationEvidence
    semantic_fingerprint: str
    terminal_checkpoint: bool
    reference_sets_agree: bool
    evidence_integrity: bool
    current_sections_complete: bool


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
        description="Persist and qualify Arun live collection.",
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
    if (end - start).days != _INCLUSIVE_WINDOW_SPAN_DAYS:
        raise QualificationConfigError(_THIRTY_DAY_WINDOW_REQUIRED)
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


def _terminal_inventory(
    store: SqliteStore,
    scope: QualificationScope,
) -> tuple[QualificationQuery, ...] | None:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = ArunCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None
    cursor = checkpoint.cursor
    expected_scope = ArunDiscoveryScope.model_validate(scope.model_dump())
    expected_plan = _canonical_query_plan(expected_scope)
    if (
        not isinstance(cursor, ArunLiveCursor)
        or cursor.scope != expected_scope
        or cursor.plan != expected_plan
        or not isinstance(cursor.progress, ArunComplete)
        or set(cursor.progress.seen_references) != set(state.references)
        or len(cursor.progress.seen_references) != len(state.references)
    ):
        return None
    return tuple(
        QualificationQuery(
            query=query,
            reported_count=completed.reported_count,
            enumerated_count=completed.enumerated_count,
        )
        for query, completed in zip(
            cursor.plan,
            cursor.progress.completed,
            strict=True,
        )
    )


def _references(store: SqliteStore) -> QualificationReferences:
    discovered = store.discovery_state(_AUTHORITY_ID).references
    retained = tuple(
        record
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    )
    return QualificationReferences(
        discovered=tuple(sorted(discovered)),
        retained_native=tuple(
            sorted(record.reference.reference for record in retained)
        ),
        applications=tuple(
            sorted(
                store.get_application(record.application_id).reference
                for record in retained
            )
        ),
    )


def _evidence_and_sections(
    store: SqliteStore,
) -> tuple[QualificationEvidence, bool, bool]:
    retained = tuple(
        record
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    )
    captures = tuple(capture for record in retained for capture in record.evidence)
    integrity = all(
        sha256(capture.body).hexdigest() == str(capture.digest) for capture in captures
    )
    sections_complete = True
    for record in retained:
        native = ArunApplicationV1.model_validate_json(record.native_json)
        current = store.get_application(record.application_id)
        sections_complete = sections_complete and (
            record.completeness.application.kind == "complete"
            and record.completeness.documents.kind in {"complete", "empty"}
            and current.completeness.application.kind == "complete"
            and current.completeness.documents.kind in {"complete", "empty"}
            and len(record.evidence) == _EVIDENCE_PER_APPLICATION
            and all(document.source_links for document in native.documents)
        )
    return (
        QualificationEvidence(
            capture_count=len(captures),
            digests=tuple(sorted(str(capture.digest) for capture in captures)),
        ),
        integrity,
        sections_complete,
    )


def _fingerprint(store: SqliteStore, snapshot: QualificationSnapshot) -> str:
    discovery = store.discovery_state(_AUTHORITY_ID)
    retained = tuple(
        record
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    )
    payload = {
        "checkpoint": None
        if discovery.checkpoint is None
        else json.loads(discovery.checkpoint.payload_json),
        "discovery": [item.model_dump(mode="json") for item in discovery.queued],
        "retained": [
            {
                "reference": record.reference.model_dump(mode="json"),
                "native_schema": record.native_schema,
                "native_json": json.loads(record.native_json),
                "completeness": record.completeness.model_dump(mode="json"),
                "evidence": [str(capture.digest) for capture in record.evidence],
            }
            for record in retained
        ],
        "applications": [
            store.get_application(record.application_id).model_dump(mode="json")
            for record in retained
        ],
        "snapshot": snapshot.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def _state(store: SqliteStore, scope: QualificationScope) -> _QualificationState:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    inventory = _terminal_inventory(store, scope)
    references = _references(store)
    evidence, evidence_integrity, current_sections_complete = _evidence_and_sections(
        store
    )
    return _QualificationState(
        snapshot=snapshot,
        query_inventory=inventory or (),
        references=references,
        evidence=evidence,
        semantic_fingerprint=_fingerprint(store, snapshot),
        terminal_checkpoint=inventory is not None,
        reference_sets_agree=(
            bool(references.discovered)
            and references.discovered == references.retained_native
            and references.discovered == references.applications
        ),
        evidence_integrity=evidence_integrity,
        current_sections_complete=current_sections_complete,
    )


def _base_checks(
    store: SqliteStore,
    state: _QualificationState,
    scope: QualificationScope,
    initial: QualificationCost,
    registry: AuthorityRegistry,
) -> tuple[QualificationCheck, ...]:
    snapshot = state.snapshot
    return (
        QualificationCheck(name="terminal-checkpoint", ok=state.terminal_checkpoint),
        QualificationCheck(name="exact-reference-sets", ok=state.reference_sets_agree),
        QualificationCheck(name="pending-retries", ok=snapshot.pending_retries == 0),
        QualificationCheck(name="failed-sections", ok=snapshot.failed_sections == 0),
        QualificationCheck(
            name="attachment-policy",
            ok=initial.attachment_body_requests == 0,
        ),
        QualificationCheck(
            name="database-integrity",
            ok=store.database_integrity() == "ok",
        ),
        QualificationCheck(
            name="evidence-paths",
            ok=not store.missing_evidence_paths(),
        ),
        QualificationCheck(name="evidence-digests", ok=state.evidence_integrity),
        QualificationCheck(
            name="evidence-capture-count",
            ok=(
                state.evidence.capture_count == snapshot.applications * 2
                and snapshot.applications > 0
            ),
        ),
        QualificationCheck(
            name="current-sections",
            ok=state.current_sections_complete,
        ),
        QualificationCheck(
            name="application-count",
            ok=(
                snapshot.applications > 0
                and snapshot.applications == snapshot.discovered_references
            ),
        ),
        QualificationCheck(name="unmapped-records", ok=snapshot.unmapped_records == 0),
        QualificationCheck(
            name="inclusive-thirty-day-scope",
            ok=(scope.end - scope.start).days == _INCLUSIVE_WINDOW_SPAN_DAYS,
        ),
        QualificationCheck(
            name="registry-unpromoted",
            ok=(
                registry.manifest(_AUTHORITY_ID).live_status.readiness
                == LiveReadiness.DISCOVERY_ONLY
            ),
        ),
    )


def _require(checks: tuple[QualificationCheck, ...]) -> None:
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise QualificationFailedError(failed)


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> ArunQualificationReceiptV1:
    registry = AuthorityRegistry((ARUN_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    prior_status_count = len(store.run_statuses())
    initial_cost = await _collect_once(collector, window, session_factory)
    initial_state = _state(store, config.scope)
    initial_checks = _base_checks(
        store,
        initial_state,
        config.scope,
        initial_cost,
        registry,
    )
    _require(initial_checks)

    rerun_cost = await _collect_once(collector, window, session_factory)
    final_state = _state(store, config.scope)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(
            store,
            final_state,
            config.scope,
            initial_cost,
            registry,
        ),
        QualificationCheck(
            name="semantic-fingerprint",
            ok=initial_state.semantic_fingerprint == final_state.semantic_fingerprint,
        ),
        QualificationCheck(
            name="idempotent-rerun",
            ok=initial_state.snapshot == final_state.snapshot,
        ),
        QualificationCheck(
            name="terminal-rerun-io",
            ok=(
                rerun_cost.request_count == 0
                and rerun_cost.transferred_bytes == 0
                and rerun_cost.attachment_body_requests == 0
            ),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    return ArunQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        query_inventory=final_state.query_inventory,
        references=final_state.references,
        evidence=final_state.evidence,
        counts=_counts(final_state.snapshot),
        semantic_fingerprint=final_state.semantic_fingerprint,
        costs=QualificationCosts(initial=initial_cost, rerun=rerun_cost),
        run_statuses=run_statuses,
        weekly_cycles=(
            WeeklyCycle(target_date=config.scope.end + timedelta(days=7)),
            WeeklyCycle(target_date=config.scope.end + timedelta(days=14)),
        ),
        checks=final_checks,
    )


def _write_receipt(path: Path, receipt: ArunQualificationReceiptV1) -> None:
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
    return HttpxPortalSession(limiter=HostRateLimiter(2.0))


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
