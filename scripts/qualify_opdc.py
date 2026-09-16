# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist and qualify OPDC's official Agile Citizen Portal collection."""

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
from urllib.parse import urlsplit

from pydantic import Field

from yimby.authorities.opdc import OPDC_PACKAGE
from yimby.authorities.opdc.adapter import (
    API_BASE_URL,
    SOURCE,
    OpdcCheckpointV1,
    OpdcDiscoveryQuery,
    OpdcDiscoveryScope,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    FrozenModel,
    LiveReadiness,
    LiveTransportKind,
    QualificationSnapshot,
    RunStatus,
    SourceReference,
)
from yimby.evidence import EvidenceIntegrityError, EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import PILOT_LIVE_STATUS, AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("opdc")
_RECEIPT_NAME = "opdc-qualification-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_SCOPE_MISMATCH = "scope-mismatch"
_LIVE_PAGE = "live"
_ATTACHMENT_PATH = "/api/application/document/opdc/"
_APPLICATION_EVIDENCE_COUNT = 3

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationScope(FrozenModel):
    """The exact inclusive 30-day discovery scope proven by the receipt."""

    start: date
    end: date
    include_open: Literal[True] = True


class QualificationIdentity(FrozenModel):
    """One exact source, public reference, and Agile locator agreement."""

    source_id: Literal["opdc-agile-applications"] = "opdc-agile-applications"
    reference: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class QualificationCounts(FrozenModel):
    """Durable OPDC counts after collection."""

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
    """Observable transport cost for the initial qualification pass."""

    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class ZeroNetworkCost(FrozenModel):
    """A typed proof that the immediate rerun performed no source I/O."""

    request_count: Literal[0]
    transferred_bytes: Literal[0]
    attachment_body_requests: Literal[0]


class QualificationCosts(FrozenModel):
    """Initial collection cost and its zero-network rerun proof."""

    initial: QualificationCost
    rerun: ZeroNetworkCost


class QualificationCheck(FrozenModel):
    """One named, falsifiable acceptance invariant."""

    name: str
    ok: bool


class QualificationQuery(FrozenModel):
    """Compact receipt summary of one identity-backed discovery query."""

    query: OpdcDiscoveryQuery
    result_total: int = Field(ge=0)


class OpdcQualificationReceiptV1(FrozenModel):
    """Versioned success proof for one complete OPDC bootstrap."""

    schema_version: Literal[1] = 1
    authority_id: Literal["opdc"] = "opdc"
    source_contract: Literal["agile-citizen-portal-v1"] = "agile-citizen-portal-v1"
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[QualificationQuery, ...]
    identities: tuple[QualificationIdentity, ...]
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
        """Retain stable failed invariant names without source bodies."""
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify OPDC live collection.",
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--data-dir", required=True)
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
        end = date.fromisoformat(arguments.end)
    except ValueError as error:
        raise QualificationConfigError(_INVALID_DATE) from error
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(
        data_dir=data_dir,
        scope=QualificationScope(
            start=end - timedelta(days=29),
            end=end,
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


def _expected_queries() -> tuple[OpdcDiscoveryQuery, ...]:
    return (
        OpdcDiscoveryQuery.REGISTERED_WINDOW,
        OpdcDiscoveryQuery.DETERMINED_WINDOW,
        OpdcDiscoveryQuery.REGISTERED_OPEN,
    )


def _expected_scope(scope: QualificationScope) -> OpdcDiscoveryScope:
    return OpdcDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=True,
    )


def _checkpoint(store: SqliteStore) -> OpdcCheckpointV1 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        return OpdcCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None


def _identity(reference: SourceReference) -> tuple[str, str, str | None]:
    return str(reference.source_id), reference.reference, reference.locator


def _checkpoint_identities(
    checkpoint: OpdcCheckpointV1,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (str(SOURCE), identity.reference, identity.locator)
        for identity in checkpoint.seen_references
    )


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> OpdcCheckpointV1 | None:
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        return None
    completed = tuple(item.query for item in checkpoint.completed_queries)
    checkpoint_identities = _checkpoint_identities(checkpoint)
    state = store.discovery_state(_AUTHORITY_ID)
    durable = tuple(_identity(reference) for reference in state.queued)
    try:
        retained = tuple(
            _identity(record.reference)
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
        )
    except (EvidenceIntegrityError, KeyError):
        return None
    if not (
        checkpoint.page_token == _LIVE_PAGE
        and checkpoint.live_scope == _expected_scope(scope)
        and checkpoint.live_complete
        and completed == _expected_queries()
        and bool(checkpoint_identities)
        and len(checkpoint_identities) == len(set(checkpoint_identities))
        and set(checkpoint_identities) == set(durable) == set(retained)
        and len(durable) == len(set(durable))
        and len(retained) == len(set(retained))
    ):
        return None
    return checkpoint


def _scope_compatible(store: SqliteStore, scope: QualificationScope) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    if state.checkpoint is None:
        return True
    checkpoint = _checkpoint(store)
    if checkpoint is None or checkpoint.live_scope is None:
        return True
    return checkpoint.live_scope == _expected_scope(scope)


def _application_evidence(
    store: SqliteStore,
    checkpoint: OpdcCheckpointV1 | None,
) -> bool:
    if checkpoint is None:
        return False
    expected_identities = set(_checkpoint_identities(checkpoint))
    try:
        records = tuple(
            record
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
        )
    except (EvidenceIntegrityError, KeyError):
        return False
    if {_identity(record.reference) for record in records} != expected_identities:
        return False
    for record in records:
        locator = record.reference.locator
        captures = record.evidence
        if locator is None or len(captures) != _APPLICATION_EVIDENCE_COUNT:
            return False
        root = f"{API_BASE_URL}/api/application/{locator}"
        actual_urls = tuple(str(capture.url) for capture in captures)
        if actual_urls != (root, f"{root}/document", f"{root}/responses") or any(
            _ATTACHMENT_PATH in urlsplit(url).path.casefold() for url in actual_urls
        ):
            return False
    return True


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
    checkpoint = _terminal_checkpoint(store, scope)
    authority = next(
        (
            state
            for state in store.authority_states()
            if state.manifest.id == _AUTHORITY_ID
        ),
        None,
    )
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=checkpoint is not None,
        ),
        QualificationCheck(
            name="reference-application-agreement",
            ok=(
                checkpoint is not None
                and snapshot.applications == len(checkpoint.seen_references)
            ),
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
            name="authority-readiness",
            ok=(
                authority is not None
                and authority.manifest.live_status.readiness == LiveReadiness.LIVE_READY
                and authority.manifest.live_status.transport == LiveTransportKind.HTTP
            ),
        ),
        QualificationCheck(
            name="evidence-paths",
            ok=not store.missing_evidence_paths(),
        ),
        QualificationCheck(
            name="evidence-integrity",
            ok=not store.invalid_evidence_paths(),
        ),
        QualificationCheck(
            name="application-evidence",
            ok=_application_evidence(store, checkpoint),
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
) -> OpdcQualificationReceiptV1:
    registry = AuthorityRegistry((OPDC_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
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
                and rerun.attachment_body_requests == 0
            ),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    terminal = _terminal_checkpoint(store, config.scope)
    if terminal is None:
        raise QualificationFailedError(("terminal-checkpoint",))
    identities = tuple(
        QualificationIdentity(
            reference=identity.reference,
            locator=identity.locator,
        )
        for identity in terminal.seen_references
    )
    return OpdcQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        query_inventory=tuple(
            QualificationQuery(query=item.query, result_total=item.result_total)
            for item in terminal.completed_queries
        ),
        identities=identities,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(
            initial=initial,
            rerun=ZeroNetworkCost.model_validate(rerun.model_dump()),
        ),
        run_statuses=run_statuses,
        checks=final_checks,
    )


def _write_receipt(
    path: Path,
    receipt: OpdcQualificationReceiptV1,
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
    """Run explicit live qualification and emit its atomic typed receipt."""
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
                if not _scope_compatible(store, config.scope):
                    raise QualificationConfigError(_SCOPE_MISMATCH)
                receipt = asyncio.run(_qualify(store, config, session_factory, now))
                _write_receipt(config.data_dir / _RECEIPT_NAME, receipt)
            finally:
                store.close()
    except QualificationConfigError as error:
        return _error(str(error), 2)
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
