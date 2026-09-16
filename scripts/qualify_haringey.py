# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Haringey live collection without overstating source completeness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import Field

from yimby.authorities.haringey import HARINGEY_PACKAGE
from yimby.authorities.haringey.adapter import (
    HaringeyApplicationPagesV1,
    HaringeyCheckpointV1,
    HaringeyLocatorV1,
    HaringeyOlderOpenUnavailableError,
    HaringeySearchPageV1,
    HaringeyWindowUnavailableError,
)
from yimby.authorities.haringey.page_object import HaringeyPlaywrightSession
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    EvidenceCapture,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalRequest, PortalSession

_AUTHORITY_ID = AuthorityId("haringey")
_RECEIPT_NAME = "haringey-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_EXACT_WINDOW_REQUIRED = "30-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_MAP_QUERY_INVENTORY = (
    "map|mapsources/AllMaps|planning_current_apps",
    "map|mapsources/WebTeam|curr_planning_apps_solo",
    "map|mapsources/WebTeam|decided_planning_apps_solo",
)

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
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    """First collection and immediate idempotence proof costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class WeeklyRefreshObligationV1(FrozenModel):
    """One genuinely later run still required after bootstrap."""

    ordinal: Literal[1, 2]
    minimum_days_after_bootstrap: Literal[7, 14]
    due_at: datetime
    status: Literal["pending"] = "pending"
    requires_genuinely_later_run: Literal[True] = True


class HaringeyQualificationReceiptV1(FrozenModel):
    """Versioned result of a complete local live qualification."""

    schema_version: Literal[1] = 1
    authority_id: Literal["haringey"] = "haringey"
    created_at: datetime
    scope: QualificationScope
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    weekly_refresh_obligations: tuple[
        WeeklyRefreshObligationV1,
        WeeklyRefreshObligationV1,
    ]


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


class QualificationSourceIncompleteError(QualificationFailedError):
    """The live source cannot prove the requested complete scope."""

    def __init__(self, failed_checks: tuple[str, ...], source_error: str) -> None:
        """Retain the failed completeness checks and source boundary."""
        super().__init__(failed_checks)
        self.source_error = source_error


class _LazyHaringeySession:
    """Delay browser launch until the adapter actually requests a page."""

    def __init__(self) -> None:
        self._session: HaringeyPlaywrightSession | None = None

    async def _get(self) -> HaringeyPlaywrightSession:
        if self._session is None:
            self._session = await HaringeyPlaywrightSession.create()
        return self._session

    async def validated_last_seven_days(self, page_number: int) -> HaringeySearchPageV1:
        return await (await self._get()).validated_last_seven_days(page_number)

    async def application_pages(
        self, locator: HaringeyLocatorV1
    ) -> HaringeyApplicationPagesV1:
        return await (await self._get()).application_pages(locator)

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        return await (await self._get()).fetch(request)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return () if self._session is None else self._session.requested_urls

    @property
    def attachment_body_requests(self) -> int:
        return 0 if self._session is None else self._session.attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        return 0 if self._session is None else self._session.transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        return 0 if self._session is None else self._session.browser_time_ms

    @property
    def mode(self) -> TransportMode:
        return TransportMode.BROWSER

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify Haringey live collection.",
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
    if end - start != timedelta(days=29):
        raise QualificationConfigError(_EXACT_WINDOW_REQUIRED)
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
            attachment_body_requests=report.attachment_body_requests,
        )
    finally:
        await session.aclose()


async def _collect_required(
    collector: Collector,
    window: DiscoveryWindow,
    session_factory: SessionFactory,
) -> QualificationCost:
    try:
        return await _collect_once(collector, window, session_factory)
    except HaringeyWindowUnavailableError as error:
        raise QualificationSourceIncompleteError(
            ("bounded-30-day-discovery", "complete-older-open-inventory"),
            type(error).__name__,
        ) from error
    except HaringeyOlderOpenUnavailableError as error:
        raise QualificationSourceIncompleteError(
            ("complete-older-open-inventory",),
            type(error).__name__,
        ) from error


def _terminal_checkpoint(store: SqliteStore, scope: QualificationScope) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return False
    try:
        checkpoint = HaringeyCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return False
    seen = checkpoint.seen_references
    durable = state.references
    return (
        checkpoint.page_token == "complete"  # noqa: S105 - checkpoint cursor.
        and checkpoint.window_start == scope.start
        and checkpoint.window_end == scope.end
        and checkpoint.quick_link_complete
        and checkpoint.reported_page_count is not None
        and checkpoint.reported_result_count is not None
        and checkpoint.next_page == checkpoint.reported_page_count + 1
        and checkpoint.observed_result_count == checkpoint.reported_result_count
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen) == set(durable)
        and _checkpoint_inventory_complete(
            checkpoint,
            scope,
            durable,
            store.valid_evidence_digests(),
        )
    )


def _expected_query_inventory(scope: QualificationScope) -> tuple[str, ...]:
    weekly: list[str] = []
    query_start = scope.start
    while query_start <= scope.end:
        query_end = min(query_start + timedelta(days=7), scope.end)
        weekly.append(f"weekly|{query_start.isoformat()}|{query_end.isoformat()}")
        query_start += timedelta(days=7)
    return (*weekly, *_MAP_QUERY_INVENTORY)


def _checkpoint_inventory_complete(
    checkpoint: HaringeyCheckpointV1,
    scope: QualificationScope,
    durable_references: tuple[str, ...],
    valid_evidence_digests: frozenset[str],
) -> bool:
    completed = checkpoint.completed_queries
    query_keys = tuple(query.key for query in completed)
    legacy_pkids = checkpoint.legacy_current_pkids
    resolutions = checkpoint.legacy_resolutions
    resolved_pkids = tuple(resolution.pkid for resolution in resolutions)
    durable = set(durable_references)
    return (
        query_keys == _expected_query_inventory(scope)
        and len(query_keys) == len(set(query_keys))
        and all(
            query.advertised_count == query.observed_count == query.unique_count
            for query in completed
        )
        and bool(legacy_pkids)
        and len(legacy_pkids) == len(set(legacy_pkids))
        and not checkpoint.ambiguous_legacy_pkids
        and len(resolved_pkids) == len(set(resolved_pkids))
        and set(resolved_pkids) == set(legacy_pkids)
        and all(resolution.public_reference in durable for resolution in resolutions)
        and all(
            resolution.evidence_digest in valid_evidence_digests
            for resolution in resolutions
        )
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
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=_terminal_checkpoint(store, scope),
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
            name="evidence-integrity",
            ok=not store.invalid_evidence_paths(),
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
) -> HaringeyQualificationReceiptV1:
    collector = Collector(AuthorityRegistry((HARINGEY_PACKAGE,)), store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    prior_status_count = len(store.run_statuses())
    initial = await _collect_required(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    initial_checks = _base_checks(store, first_snapshot, config.scope, initial)
    _require(initial_checks)

    rerun = await _collect_required(collector, window, session_factory)
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
    created_at = now()
    return HaringeyQualificationReceiptV1(
        created_at=created_at,
        scope=config.scope,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_refresh_obligations=_pending_weekly_obligations(created_at),
    )


def _pending_weekly_obligations(
    bootstrap_at: datetime,
) -> tuple[WeeklyRefreshObligationV1, WeeklyRefreshObligationV1]:
    return (
        WeeklyRefreshObligationV1(
            ordinal=1,
            minimum_days_after_bootstrap=7,
            due_at=bootstrap_at + timedelta(days=7),
        ),
        WeeklyRefreshObligationV1(
            ordinal=2,
            minimum_days_after_bootstrap=14,
            due_at=bootstrap_at + timedelta(days=14),
        ),
    )


def _write_receipt(path: Path, receipt: HaringeyQualificationReceiptV1) -> None:
    payload = f"{receipt.model_dump_json(indent=2)}\n"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def _default_session() -> PortalSession:
    return _LazyHaringeySession()


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
    except QualificationSourceIncompleteError as error:
        return _error(
            "qualification-failed",
            1,
            failed_checks=list(error.failed_checks),
            source_error=error.source_error,
        )
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
