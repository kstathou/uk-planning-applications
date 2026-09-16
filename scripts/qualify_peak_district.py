# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Peak District live collection without promoting registry readiness."""

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

from yimby.authorities.peak_district import PEAK_DISTRICT_PACKAGE
from yimby.authorities.peak_district.adapter import (
    LEGACY_SOURCE,
    PeakDistrictCheckpointV1,
    PeakDistrictDiscoveryScope,
    PeakDistrictQueryV1,
    peak_district_query_inventory,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("peak-district")
_RECEIPT_NAME = "peak-district-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_THIRTY_DAYS_REQUIRED = "thirty-days-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_INCLUSIVE_WINDOW_DAYS = 30

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationScope(FrozenModel):
    """Exact inclusive discovery scope proven by the receipt."""

    start: date
    end: date
    include_open: bool


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
    """Bootstrap and immediate idempotence proof costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class RetryPolicy(FrozenModel):
    """Transport retry policy used by the live qualification command."""

    max_attempts: Literal[1] = 1


class WeeklyCycle(FrozenModel):
    """One genuinely later weekly cycle that cannot complete on bootstrap day."""

    cycle: Literal[1, 2]
    eligible_on: date
    status: Literal["pending"] = "pending"


class PeakDistrictQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local Peak District bootstrap."""

    schema_version: Literal[1] = 1
    authority_id: Literal["peak-district"] = "peak-district"
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[PeakDistrictQueryV1, ...]
    retry_policy: RetryPolicy = RetryPolicy()
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    weekly_cycles: tuple[WeeklyCycle, WeeklyCycle]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope


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
        description="Persist and qualify Peak District live collection.",
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
    if (end - start).days + 1 != _INCLUSIVE_WINDOW_DAYS:
        raise QualificationConfigError(_THIRTY_DAYS_REQUIRED)
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(
        data_dir=data_dir,
        scope=QualificationScope(start=start, end=end, include_open=True),
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


def _checkpoint(
    store: SqliteStore,
) -> PeakDistrictCheckpointV1 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        return PeakDistrictCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None


def _expected_scope(scope: QualificationScope) -> PeakDistrictDiscoveryScope:
    return PeakDistrictDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )


def _expected_inventory(
    scope: QualificationScope,
) -> tuple[PeakDistrictQueryV1, ...]:
    return peak_district_query_inventory(
        DiscoveryWindow(
            start=scope.start,
            end=scope.end,
            include_open=scope.include_open,
        )
    )


def _exact_query_inventory(
    checkpoint: PeakDistrictCheckpointV1 | None,
    scope: QualificationScope,
) -> bool:
    if checkpoint is None:
        return False
    expected = tuple(query.key for query in _expected_inventory(scope))
    return checkpoint.completed_queries == expected


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> bool:
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        return False
    seen = checkpoint.seen_references
    durable = store.discovery_state(_AUTHORITY_ID).references
    return (
        checkpoint.row_offset == "live"
        and checkpoint.live_scope == _expected_scope(scope)
        and checkpoint.live_complete
        and _exact_query_inventory(checkpoint, scope)
        and checkpoint.active_query is None
        and checkpoint.next_page_index == 0
        and checkpoint.query_row_count == 0
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen) == set(durable)
    )


def _reference_application_agreement(store: SqliteStore) -> bool:
    checkpoint = _checkpoint(store)
    if checkpoint is None or not checkpoint.seen_references:
        return False
    expected = {
        (str(LEGACY_SOURCE), reference) for reference in checkpoint.seen_references
    }
    queued = {
        (str(reference.source_id), reference.reference)
        for reference in store.discovery_state(_AUTHORITY_ID).queued
    }
    retained = {
        (str(record.reference.source_id), record.reference.reference)
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    }
    return expected == queued == retained


def _evidence_integrity(store: SqliteStore) -> bool:
    records = tuple(
        record
        for record in store.retained_native_records()
        if record.authority_id == _AUTHORITY_ID
    )
    return bool(records) and all(
        record.evidence
        and all(
            sha256(capture.body).hexdigest() == str(capture.digest)
            for capture in record.evidence
        )
        for record in records
    )


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _base_checks(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
    scope: QualificationScope,
    initial: QualificationCost,
) -> tuple[QualificationCheck, ...]:
    checkpoint = _checkpoint(store)
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=_terminal_checkpoint(store, scope),
        ),
        QualificationCheck(
            name="exact-query-inventory",
            ok=_exact_query_inventory(checkpoint, scope),
        ),
        QualificationCheck(
            name="reference-application-agreement",
            ok=_reference_application_agreement(store),
        ),
        QualificationCheck(
            name="pending-retries",
            ok=snapshot.pending_retries == 0,
        ),
        QualificationCheck(
            name="failed-sections",
            ok=snapshot.failed_sections == 0,
        ),
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
        QualificationCheck(
            name="evidence-integrity",
            ok=_evidence_integrity(store),
        ),
        QualificationCheck(
            name="application-count",
            ok=(
                snapshot.applications > 0
                and snapshot.applications == snapshot.discovered_references
            ),
        ),
        QualificationCheck(
            name="unmapped-records",
            ok=snapshot.unmapped_records == 0,
        ),
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
) -> PeakDistrictQualificationReceiptV1:
    registry = AuthorityRegistry((PEAK_DISTRICT_PACKAGE,))
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    prior_status_count = len(store.run_statuses())
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    initial_checks = _base_checks(store, first_snapshot, config.scope, initial)
    _require(initial_checks)

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(store, final_snapshot, config.scope, initial),
        QualificationCheck(
            name="idempotent-rerun",
            ok=first_snapshot == final_snapshot,
        ),
        QualificationCheck(
            name="terminal-rerun-io",
            ok=(
                rerun.request_count == 0
                and rerun.transferred_bytes == 0
                and rerun.attachment_body_requests == 0
            ),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    created_at = now()
    return PeakDistrictQualificationReceiptV1(
        created_at=created_at,
        scope=config.scope,
        query_inventory=_expected_inventory(config.scope),
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_cycles=(
            WeeklyCycle(
                cycle=1,
                eligible_on=created_at.date() + timedelta(days=7),
            ),
            WeeklyCycle(
                cycle=2,
                eligible_on=created_at.date() + timedelta(days=14),
            ),
        ),
    )


def _write_receipt(
    path: Path,
    receipt: PeakDistrictQualificationReceiptV1,
) -> None:
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
