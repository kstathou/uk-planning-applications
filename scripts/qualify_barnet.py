# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D100, D101, D103, D107, INP001, T201

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import Field

from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.barnet.adapter import (
    CURRENT_SOURCE,
    BarnetCheckpointError,
    BarnetCheckpointV1,
    BarnetDiscoveryScope,
    BarnetParseError,
    BarnetReferenceMismatchError,
    BarnetRoutingError,
    expected_live_query_keys,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    FrozenModel,
    QualificationLineage,
    QualificationSnapshot,
    RetainedNativeRecord,
    RunStatus,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.orchestration import CollectionAlreadyRunningError, ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalSession,
    SourceUnavailableError,
)

_AUTHORITY_ID = AuthorityId("barnet")
_RECEIPT_NAME = "barnet-qualification-v1.json"
_QUALIFICATION_NAME = "barnet-live-v1"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_THIRTY_DAY_WINDOW_REQUIRED = "thirty-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_RECEIPT_ANCHOR_REQUIRED = "receipt-anchor-required"
_INCLUSIVE_WINDOW_SPAN_DAYS = 29
_BARNET_MINIMUM_GAP_SECONDS = 10.0
_BARNET_MAX_ATTEMPTS = 1

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationCounts(FrozenModel):
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
    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    name: str
    ok: bool


class PendingWeeklyRefresh(FrozenModel):
    ordinal: Literal[1, 2]
    due_on: date
    status: Literal["pending"] = "pending"


class BarnetQualificationReceiptV1(FrozenModel):
    schema_version: Literal[1] = 1
    authority_id: Literal["barnet"] = "barnet"
    created_at: datetime
    scope: BarnetDiscoveryScope
    query_inventory: tuple[str, ...]
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    weekly_refreshes: tuple[PendingWeeklyRefresh, PendingWeeklyRefresh]


class _Config(FrozenModel):
    data_dir: Path
    scope: BarnetDiscoveryScope


class QualificationConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class QualificationFailedError(RuntimeError):
    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


class QualificationAnchorError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify a 30-day Barnet live bootstrap.",
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
        scope=BarnetDiscoveryScope(
            start=start,
            end=end,
            include_open=True,
        ),
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
) -> BarnetCheckpointV1 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        return BarnetCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None


def _terminal_checkpoint(
    store: SqliteStore,
    scope: BarnetDiscoveryScope,
) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        return False
    expected_queries = expected_live_query_keys(scope)
    seen = checkpoint.seen_references
    locators = checkpoint.seen_locators
    queued = state.queued
    queued_locators = {reference.reference: reference.locator for reference in queued}
    return (
        checkpoint.cursor == "live"
        and checkpoint.live_scope == scope
        and checkpoint.live_complete
        and checkpoint.completed_queries == expected_queries
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and checkpoint.query_reported_count is None
        and not checkpoint.active_query_references
        and bool(queued)
        and all(
            reference.source_id == CURRENT_SOURCE and reference.locator
            for reference in queued
        )
        and len(seen) == len(set(seen))
        and set(seen) == {reference.reference for reference in queued}
        and len(locators) <= len(seen)
        and (
            not checkpoint.tracks_locators
            or (
                len(locators) == len(seen)
                and all(locator is not None for locator in locators)
            )
        )
        and all(
            locator is None or queued_locators.get(reference) == locator
            for reference, locator in zip(seen, locators, strict=False)
        )
    )


def _query_inventory(
    store: SqliteStore,
    scope: BarnetDiscoveryScope,
    expected: tuple[str, ...],
) -> bool:
    checkpoint = _checkpoint(store)
    return (
        expected == expected_live_query_keys(scope)
        and checkpoint is not None
        and checkpoint.completed_queries == expected
    )


def _retained_records(
    store: SqliteStore,
) -> tuple[RetainedNativeRecord, ...] | None:
    try:
        return tuple(
            record
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
        )
    except (EOFError, KeyError, OSError, ValueError):
        return None


def _evidence_integrity(store: SqliteStore) -> bool:
    try:
        captures = store.retained_evidence_captures()
    except (EOFError, KeyError, OSError, ValueError):
        return False
    return bool(captures) and all(
        sha256(capture.body).hexdigest() == str(capture.digest) for capture in captures
    )


def _reference_application_agreement(store: SqliteStore) -> bool:
    queued = store.discovery_state(_AUTHORITY_ID).queued
    records = _retained_records(store)
    if not queued or records is None:
        return False
    queued_keys = tuple(
        (str(reference.source_id), reference.reference, reference.locator)
        for reference in queued
    )
    record_keys = tuple(
        (
            str(record.reference.source_id),
            record.reference.reference,
            record.reference.locator,
        )
        for record in records
    )
    return (
        all(locator for _source, _reference, locator in queued_keys)
        and len(queued_keys) == len(set(queued_keys))
        and len(record_keys) == len(set(record_keys))
        and set(queued_keys) == set(record_keys)
    )


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _base_checks(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
    scope: BarnetDiscoveryScope,
    inventory: tuple[str, ...],
    initial: QualificationCost,
) -> tuple[QualificationCheck, ...]:
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=_terminal_checkpoint(store, scope),
        ),
        QualificationCheck(
            name="query-inventory",
            ok=_query_inventory(store, scope, inventory),
        ),
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
            name="reference-application-agreement",
            ok=_reference_application_agreement(store),
        ),
        QualificationCheck(name="unmapped-records", ok=snapshot.unmapped_records == 0),
    )


def _require(checks: tuple[QualificationCheck, ...]) -> None:
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise QualificationFailedError(failed)


def _receipt_anchor(
    receipt: BarnetQualificationReceiptV1 | None,
    scope: BarnetDiscoveryScope,
    current_time: datetime,
) -> datetime | None:
    if receipt is None or receipt.scope != scope:
        return None
    created_at = receipt.created_at
    if created_at.tzinfo is None or created_at > current_time:
        return None
    expected_due_dates = (
        created_at.date() + timedelta(days=7),
        created_at.date() + timedelta(days=14),
    )
    if (
        tuple(refresh.due_on for refresh in receipt.weekly_refreshes)
        != expected_due_dates
    ):
        return None
    return created_at


def _lineage_anchor(
    lineage: QualificationLineage | None,
    scope: BarnetDiscoveryScope,
    current_time: datetime,
) -> datetime | None:
    if lineage is None:
        return None
    try:
        stored_scope = BarnetDiscoveryScope.model_validate_json(lineage.scope_json)
    except ValueError:
        return None
    if stored_scope != scope:
        return None
    if lineage.created_at.tzinfo is None or lineage.created_at > current_time:
        return None
    return lineage.created_at


def _resolve_anchor(
    lineage: QualificationLineage | None,
    receipt: BarnetQualificationReceiptV1 | None,
    scope: BarnetDiscoveryScope,
    current_time: datetime,
) -> datetime | None:
    stored = _lineage_anchor(lineage, scope, current_time)
    receipted = _receipt_anchor(receipt, scope, current_time)
    if stored is not None and receipted is not None and stored != receipted:
        raise QualificationAnchorError
    return stored if stored is not None else receipted


def _record_lineage(
    store: SqliteStore,
    scope: BarnetDiscoveryScope,
    created_at: datetime,
) -> QualificationLineage:
    candidate = QualificationLineage(
        authority_id=_AUTHORITY_ID,
        qualification=_QUALIFICATION_NAME,
        scope_json=scope.model_dump_json(),
        created_at=created_at,
    )
    persisted = store.record_qualification_lineage(candidate)
    if persisted != candidate:
        raise QualificationAnchorError
    return persisted


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    current_time: datetime,
    anchor: datetime | None,
) -> BarnetQualificationReceiptV1:
    registry = AuthorityRegistry((BARNET_PACKAGE,))
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    inventory = expected_live_query_keys(config.scope)
    prior_status_count = len(store.run_statuses())
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    initial_checks = _base_checks(
        store,
        first_snapshot,
        config.scope,
        inventory,
        initial,
    )
    _require(initial_checks)
    created_at = current_time if anchor is None else anchor

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(store, final_snapshot, config.scope, inventory, initial),
        QualificationCheck(
            name="idempotent-rerun",
            ok=first_snapshot == final_snapshot,
        ),
        QualificationCheck(
            name="terminal-rerun-cost",
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
    return BarnetQualificationReceiptV1(
        created_at=created_at,
        scope=config.scope,
        query_inventory=inventory,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_refreshes=(
            PendingWeeklyRefresh(
                ordinal=1,
                due_on=created_at.date() + timedelta(days=7),
            ),
            PendingWeeklyRefresh(
                ordinal=2,
                due_on=created_at.date() + timedelta(days=14),
            ),
        ),
    )


def _write_receipt(
    path: Path,
    receipt: BarnetQualificationReceiptV1,
) -> None:
    _write_payload(path, f"{receipt.model_dump_json(indent=2)}\n")


def _write_payload(path: Path, payload: str) -> None:
    temporary: Path | None = None
    replaced = False
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if replaced:
            path.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_receipt(path: Path) -> BarnetQualificationReceiptV1 | None:
    try:
        return BarnetQualificationReceiptV1.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _default_session() -> HttpxPortalSession:
    return HttpxPortalSession(
        limiter=HostRateLimiter(_BARNET_MINIMUM_GAP_SECONDS),
        max_attempts=_BARNET_MAX_ATTEMPTS,
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
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except QualificationConfigError as error:
        return _error(str(error), 2)
    receipt_path = config.data_dir / _RECEIPT_NAME
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            current_time = now()
            prior_receipt = _read_receipt(receipt_path)
            store = SqliteStore(
                config.data_dir / "yimby.sqlite3",
                EvidenceStore(config.data_dir / "evidence"),
            )
            try:
                lineage = store.qualification_lineage(
                    _AUTHORITY_ID,
                    _QUALIFICATION_NAME,
                )
                anchor = _resolve_anchor(
                    lineage,
                    prior_receipt,
                    config.scope,
                    current_time,
                )
                if lineage is not None and anchor is None:
                    raise QualificationAnchorError
                if lineage is None and anchor is not None:
                    lineage = _record_lineage(store, config.scope, anchor)
                receipt_path.unlink(missing_ok=True)
                receipt = asyncio.run(
                    _qualify(store, config, session_factory, current_time, anchor)
                )
                _record_lineage(store, receipt.scope, receipt.created_at)
                _write_receipt(receipt_path, receipt)
            finally:
                store.close()
    except QualificationFailedError as error:
        return _error(
            "qualification-failed",
            1,
            failed_checks=list(error.failed_checks),
        )
    except QualificationAnchorError:
        return _error(_RECEIPT_ANCHOR_REQUIRED, 1)
    except SourceUnavailableError as error:
        return _error("source-unavailable", 1, detail=str(error))
    except (
        AttachmentBodyBlockedError,
        BarnetCheckpointError,
        BarnetParseError,
        BarnetReferenceMismatchError,
        BarnetRoutingError,
        CollectionAlreadyRunningError,
        OSError,
        sqlite3.Error,
    ) as error:
        return _error("runtime-failure", 1, exception=type(error).__name__)
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
