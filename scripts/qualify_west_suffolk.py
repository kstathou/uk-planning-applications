# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify West Suffolk live collection without changing registry readiness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from yimby.authorities.west_suffolk import WEST_SUFFOLK_PACKAGE
from yimby.authorities.west_suffolk.adapter import (
    WestSuffolkCheckpointV1,
    WestSuffolkDiscoveryScope,
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

_AUTHORITY_ID = AuthorityId("west-suffolk")
_RECEIPT_NAME = "west-suffolk-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_WEEKLY_DATE_TYPES = ("DC_Validated", "DC_Decided")
_WEEK_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_ADVANCED_QUERY_KEYS = (
    "advanced|searchCriteria.caseStatus|Pending Consideration",
    "advanced|searchCriteria.caseStatus|Pending Decision",
    "advanced|searchCriteria.caseStatus|Received Awaiting Registration",
    "advanced|searchCriteria.caseStatus|Pending Appeal Decision",
    "advanced|searchCriteria.appealStatus|Appeal lodged",
    "advanced|searchCriteria.appealStatus|Appeal Remitted to Secretary of State ",
    "advanced|searchCriteria.appealStatus|High Court Appeal Lodged",
    "advanced|searchCriteria.appealStatus|Pending Appeal Decision",
)


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


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class WestSuffolkQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local live qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["west-suffolk"] = "west-suffolk"
    created_at: datetime
    scope: QualificationScope
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]


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
        description="Persist and qualify West Suffolk live collection.",
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


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return False
    try:
        checkpoint = WestSuffolkCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return False
    expected = WestSuffolkDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )
    seen = checkpoint.seen_references
    durable = state.references
    return (
        checkpoint.result_page == "live"
        and checkpoint.live_scope == expected
        and checkpoint.live_complete
        and _completed_query_inventory(checkpoint, scope)
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen) == set(durable)
    )


def _expected_weekly_queries(
    scope: QualificationScope,
) -> tuple[tuple[date, str], ...]:
    monday = scope.start - timedelta(days=scope.start.weekday())
    weekly: list[tuple[date, str]] = []
    while monday <= scope.end:
        if monday + timedelta(days=6) >= scope.start:
            weekly.extend((monday, date_type) for date_type in _WEEKLY_DATE_TYPES)
        monday += timedelta(days=7)
    return tuple(weekly)


def _completed_query_inventory(
    checkpoint: WestSuffolkCheckpointV1,
    scope: QualificationScope,
) -> bool:
    expected_weekly = _expected_weekly_queries(scope)
    weekly_count = len(expected_weekly)
    completed = checkpoint.completed_queries
    if completed[weekly_count:] != _ADVANCED_QUERY_KEYS:
        return False
    actual_weekly: list[tuple[date, str]] = []
    for key in completed[:weekly_count]:
        week_value, separator, date_type = key.rpartition("|")
        if separator != "|" or date_type not in _WEEKLY_DATE_TYPES:
            return False
        parsed = _parse_week_date(week_value)
        if parsed is None:
            return False
        actual_weekly.append((parsed, date_type))
    return tuple(actual_weekly) == expected_weekly


def _parse_week_date(value: str) -> date | None:
    for date_format in _WEEK_DATE_FORMATS:
        try:
            return (
                datetime.strptime(value.strip(), date_format).replace(tzinfo=UTC).date()
            )
        except ValueError:
            continue
    return None


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
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=_terminal_checkpoint(store, scope),
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
) -> WestSuffolkQualificationReceiptV1:
    registry = AuthorityRegistry((WEST_SUFFOLK_PACKAGE,))
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
    return WestSuffolkQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
    )


def _write_receipt(
    path: Path,
    receipt: WestSuffolkQualificationReceiptV1,
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
