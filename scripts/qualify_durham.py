# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Durham live collection without changing registry readiness."""

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

from yimby.authorities.durham import DURHAM_PACKAGE
from yimby.authorities.durham.adapter import (
    DurhamCheckpointV1,
    DurhamDiscoveryScope,
    durham_open_query_keys,
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

_AUTHORITY_ID = AuthorityId("durham")
_RECEIPT_NAME = "durham-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_WEEKLY_DATE_TYPES = ("DC_Validated", "DC_Decided")
_WEEK_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_ROLLING_WINDOW_DAYS = 30

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
    """First collection and immediate idempotence proof costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationInventory(FrozenModel):
    """Exact query and durable reference inventory certified by the receipt."""

    weekly_query_keys: tuple[str, ...]
    older_open_query_keys: tuple[str, ...]
    completed_query_keys: tuple[str, ...]
    reference_count: int = Field(ge=0)
    reference_sha256: str


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class PendingWeeklyCycle(FrozenModel):
    """One future weekly validation cycle that cannot yet be claimed."""

    scheduled_for: date
    status: Literal["pending"] = "pending"


class DurhamQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local Durham live qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["durham"] = "durham"
    created_at: datetime
    scope: QualificationScope
    inventory: QualificationInventory
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    future_weekly_cycles: tuple[PendingWeeklyCycle, PendingWeeklyCycle]


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
        description="Persist and qualify Durham live collection.",
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
    if (end - start).days != _ROLLING_WINDOW_DAYS - 1:
        raise QualificationConfigError(_INVALID_WINDOW)
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


def _expected_weekly_query_keys(scope: QualificationScope) -> tuple[str, ...]:
    monday = scope.start - timedelta(days=scope.start.weekday())
    weekly: list[str] = []
    while monday <= scope.end:
        if monday + timedelta(days=6) >= scope.start:
            weekly.extend(
                f"{monday.isoformat()}|{date_type}"
                for date_type in _WEEKLY_DATE_TYPES
            )
        monday += timedelta(days=7)
    return tuple(weekly)


def _parse_week_date(value: str) -> date | None:
    for date_format in _WEEK_DATE_FORMATS:
        try:
            return (
                datetime.strptime(value.strip(), date_format)
                .replace(tzinfo=UTC)
                .date()
            )
        except ValueError:
            continue
    return None


def _normalised_completed_query_keys(
    checkpoint: DurhamCheckpointV1,
    scope: QualificationScope,
) -> tuple[str, ...] | None:
    expected_weekly = _expected_weekly_query_keys(scope)
    weekly_count = len(expected_weekly)
    completed = checkpoint.completed_queries
    actual_weekly = []
    for key in completed[:weekly_count]:
        week_value, separator, date_type = key.rpartition("|")
        if separator != "|" or date_type not in _WEEKLY_DATE_TYPES:
            return None
        parsed = _parse_week_date(week_value)
        if parsed is None:
            return None
        actual_weekly.append(f"{parsed.isoformat()}|{date_type}")
    normalised = (*actual_weekly, *completed[weekly_count:])
    expected = (*expected_weekly, *durham_open_query_keys(scope.end))
    return normalised if normalised == expected else None


def _inventory(
    store: SqliteStore,
    scope: QualificationScope,
) -> QualificationInventory | None:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = DurhamCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None
    expected_scope = DurhamDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )
    completed = _normalised_completed_query_keys(checkpoint, scope)
    durable = state.references
    seen = checkpoint.seen_references
    if not (
        checkpoint.result_page == "live"
        and checkpoint.live_scope == expected_scope
        and checkpoint.live_complete
        and completed is not None
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen) == set(durable)
    ):
        return None
    ordered = tuple(sorted(durable))
    return QualificationInventory(
        weekly_query_keys=_expected_weekly_query_keys(scope),
        older_open_query_keys=durham_open_query_keys(scope.end),
        completed_query_keys=completed,
        reference_count=len(ordered),
        reference_sha256=sha256("\n".join(ordered).encode()).hexdigest(),
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
    inventory = _inventory(store, scope)
    return (
        QualificationCheck(
            name="rolling-30-day-window",
            ok=(scope.end - scope.start).days == _ROLLING_WINDOW_DAYS - 1,
        ),
        QualificationCheck(name="older-open-included", ok=scope.include_open),
        QualificationCheck(name="terminal-checkpoint", ok=inventory is not None),
        QualificationCheck(
            name="query-inventory",
            ok=(
                inventory is not None
                and inventory.completed_query_keys
                == (*inventory.weekly_query_keys, *inventory.older_open_query_keys)
            ),
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
            name="application-count",
            ok=(
                snapshot.applications > 0
                and snapshot.applications == snapshot.discovered_references
                and inventory is not None
                and snapshot.applications == inventory.reference_count
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
) -> DurhamQualificationReceiptV1:
    registry = AuthorityRegistry((DURHAM_PACKAGE,))
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
            name="terminal-rerun-requests",
            ok=(rerun.request_count == 0 and rerun.attachment_body_requests == 0),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    inventory = _inventory(store, config.scope)
    if inventory is None:
        raise QualificationFailedError(("terminal-checkpoint",))
    return DurhamQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        inventory=inventory,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        future_weekly_cycles=(
            PendingWeeklyCycle(scheduled_for=config.scope.end + timedelta(days=7)),
            PendingWeeklyCycle(scheduled_for=config.scope.end + timedelta(days=14)),
        ),
    )


def _write_receipt(path: Path, receipt: DurhamQualificationReceiptV1) -> None:
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
    return HttpxPortalSession()


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
