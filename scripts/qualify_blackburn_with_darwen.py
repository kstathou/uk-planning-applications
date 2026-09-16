# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist and qualify Blackburn with Darwen browser collection."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from yimby.authorities.blackburn_with_darwen import BLACKBURN_WITH_DARWEN_PACKAGE
from yimby.authorities.blackburn_with_darwen.adapter import (
    BlackburnDateRangeV1,
    BlackburnDiscoveryScope,
    BlackburnQueryKind,
    BlackburnQueryV1,
    BlackburnWithDarwenCheckpointV2,
)
from yimby.authorities.blackburn_with_darwen.page_object import (
    BlackburnPlaywrightSession,
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
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("blackburn-with-darwen")
_RECEIPT_NAME = "blackburn-with-darwen-qualification-v1.json"
_HISTORICAL_START = date(1977, 1, 1)
_WINDOW_DAYS = 30
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_WINDOW_REQUIRED = "window-must-be-30-days"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"

SessionFactory = Callable[[], Awaitable[PortalSession]]
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
    browser_time_ms: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    """Initial collection and immediate replay costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class QualificationQueryInventory(FrozenModel):
    """Root, terminal, and split query keys in the persisted checkpoint."""

    roots: tuple[str, ...]
    completed: tuple[str, ...]
    split: tuple[str, ...]


class WeeklyCycle(FrozenModel):
    """Later operational cycle that cannot be completed on qualification day."""

    scheduled_for: date
    status: Literal["pending"] = "pending"


class BlackburnQualifiedReceiptV1(FrozenModel):
    """Versioned proof of complete same-day live qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["blackburn-with-darwen"] = "blackburn-with-darwen"
    outcome: Literal["qualified"] = "qualified"
    created_at: datetime
    scope: QualificationScope
    counts: QualificationCounts
    costs: QualificationCosts
    query_inventory: QualificationQueryInventory
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    weekly_cycles: tuple[WeeklyCycle, ...]


class BlackburnBlockedReceiptV1(FrozenModel):
    """Versioned fail-closed result without a readiness claim."""

    schema_version: Literal[1] = 1
    authority_id: Literal["blackburn-with-darwen"] = "blackburn-with-darwen"
    outcome: Literal["blocked"] = "blocked"
    created_at: datetime
    scope: QualificationScope
    blocker_code: str
    failed_checks: tuple[str, ...] = ()
    weekly_cycles: tuple[WeeklyCycle, ...]


QualificationReceipt = BlackburnQualifiedReceiptV1 | BlackburnBlockedReceiptV1


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope


class QualificationConfigError(ValueError):
    """One required safety option or scope value is invalid."""

    def __init__(self, code: str) -> None:
        """Retain the stable command error code."""
        super().__init__(code)


class QualificationFailedError(RuntimeError):
    """One or more same-day acceptance checks failed."""

    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        """Retain stable failed check names."""
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify Blackburn with Darwen live collection."
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
    if (end - start).days + 1 != _WINDOW_DAYS:
        raise QualificationConfigError(_WINDOW_REQUIRED)
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
    session = await session_factory()
    try:
        report = await collector.collect(_AUTHORITY_ID, window, session)
        return QualificationCost(
            request_count=len(report.requested_urls),
            transferred_bytes=session.transferred_bytes,
            browser_time_ms=session.browser_time_ms,
            attachment_body_requests=report.attachment_body_requests,
        )
    finally:
        await session.aclose()


def _checkpoint(store: SqliteStore) -> BlackburnWithDarwenCheckpointV2 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        return BlackburnWithDarwenCheckpointV2.model_validate_json(stored.payload_json)
    except ValueError:
        return None


def _root_queries(scope: QualificationScope) -> tuple[BlackburnQueryV1, ...]:
    recent = BlackburnDateRangeV1(start=scope.start, end=scope.end)
    queries = [
        BlackburnQueryV1(kind=BlackburnQueryKind.RECEIVED, date_range=recent),
        BlackburnQueryV1(kind=BlackburnQueryKind.VALID, date_range=recent),
        BlackburnQueryV1(kind=BlackburnQueryKind.DECISION, date_range=recent),
    ]
    if scope.include_open and scope.start > _HISTORICAL_START:
        queries.append(
            BlackburnQueryV1(
                kind=BlackburnQueryKind.OLDER_OPEN,
                date_range=BlackburnDateRangeV1(
                    start=_HISTORICAL_START,
                    end=scope.start - timedelta(days=1),
                ),
            )
        )
    return tuple(queries)


def _children(query: BlackburnQueryV1) -> tuple[BlackburnQueryV1, ...]:
    date_range = query.date_range
    if date_range.start == date_range.end:
        return ()
    midpoint = date_range.start + (date_range.end - date_range.start) // 2
    return (
        query.model_copy(
            update={
                "date_range": BlackburnDateRangeV1(
                    start=date_range.start,
                    end=midpoint,
                )
            }
        ),
        query.model_copy(
            update={
                "date_range": BlackburnDateRangeV1(
                    start=midpoint + timedelta(days=1),
                    end=date_range.end,
                )
            }
        ),
    )


def _query_inventory_complete(
    checkpoint: BlackburnWithDarwenCheckpointV2,
    scope: QualificationScope,
) -> bool:
    completed = {query.key: query for query in checkpoint.completed_queries}
    split = {query.key: query for query in checkpoint.split_queries}
    if (
        len(completed) != len(checkpoint.completed_queries)
        or len(split) != len(checkpoint.split_queries)
        or completed.keys() & split.keys()
    ):
        return False
    expected_keys: set[str] = set()
    pending = list(_root_queries(scope))
    while pending:
        query = pending.pop()
        if query.key in expected_keys:
            return False
        expected_keys.add(query.key)
        if query.key in split:
            children = _children(query)
            if not children:
                return False
            pending.extend(children)
        elif query.key not in completed:
            return False
    return expected_keys == completed.keys() | split.keys()


def _terminal_checkpoint(store: SqliteStore, scope: QualificationScope) -> bool:
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        return False
    state = store.discovery_state(_AUTHORITY_ID)
    expected = BlackburnDiscoveryScope(
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
        and checkpoint.pending_queries == ()
        and _query_inventory_complete(checkpoint, scope)
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen) == set(durable)
    )


def _query_inventory(
    checkpoint: BlackburnWithDarwenCheckpointV2,
    scope: QualificationScope,
) -> QualificationQueryInventory:
    return QualificationQueryInventory(
        roots=tuple(query.key for query in _root_queries(scope)),
        completed=tuple(query.key for query in checkpoint.completed_queries),
        split=tuple(query.key for query in checkpoint.split_queries),
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
            name="query-inventory",
            ok=(
                checkpoint is not None and _query_inventory_complete(checkpoint, scope)
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


def _weekly_cycles(scope: QualificationScope) -> tuple[WeeklyCycle, ...]:
    return (
        WeeklyCycle(scheduled_for=scope.end + timedelta(days=7)),
        WeeklyCycle(scheduled_for=scope.end + timedelta(days=14)),
    )


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> BlackburnQualifiedReceiptV1:
    registry = AuthorityRegistry((BLACKBURN_WITH_DARWEN_PACKAGE,))
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
            ok=(
                rerun.request_count == 0
                and rerun.transferred_bytes == 0
                and rerun.browser_time_ms == 0
                and rerun.attachment_body_requests == 0
            ),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        raise QualificationFailedError(("terminal-checkpoint",))
    return BlackburnQualifiedReceiptV1(
        created_at=now(),
        scope=config.scope,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        query_inventory=_query_inventory(checkpoint, config.scope),
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_cycles=_weekly_cycles(config.scope),
    )


def _write_receipt(path: Path, receipt: QualificationReceipt) -> None:
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


async def _default_session() -> PortalSession:
    return await BlackburnPlaywrightSession.create()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _blocked_receipt(
    config: _Config,
    now: Clock,
    blocker_code: str,
    failed_checks: tuple[str, ...] = (),
) -> BlackburnBlockedReceiptV1:
    return BlackburnBlockedReceiptV1(
        created_at=now(),
        scope=config.scope,
        blocker_code=blocker_code,
        failed_checks=failed_checks,
        weekly_cycles=_weekly_cycles(config.scope),
    )


def _error(code: str, exit_code: int, **details: object) -> int:
    print(json.dumps({"error": code, **details}, sort_keys=True), file=sys.stderr)
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory = _default_session,
    now: Clock = _default_clock,
) -> int:
    """Run explicit live qualification and emit one atomic typed receipt."""
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except QualificationConfigError as error:
        return _error(str(error), 2)
    receipt_path = config.data_dir / _RECEIPT_NAME
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            store = SqliteStore(
                config.data_dir / "yimby.sqlite3",
                EvidenceStore(config.data_dir / "evidence"),
            )
            try:
                receipt = asyncio.run(_qualify(store, config, session_factory, now))
                _write_receipt(receipt_path, receipt)
            finally:
                store.close()
    except QualificationFailedError as error:
        blocked = _blocked_receipt(
            config,
            now,
            "qualification-failed",
            error.failed_checks,
        )
        _write_receipt(receipt_path, blocked)
        return _error(
            "qualification-failed",
            1,
            failed_checks=list(error.failed_checks),
        )
    except Exception as error:  # noqa: BLE001
        blocked = _blocked_receipt(config, now, type(error).__name__)
        _write_receipt(receipt_path, blocked)
        return _error("runtime-failure", 1, exception=type(error).__name__)
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
