# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Devon live collection without changing registry readiness."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from yimby.authorities.devon import DEVON_PACKAGE
from yimby.authorities.devon.adapter import (
    DevonCheckpointV1,
    DevonQualificationAuditV1,
    DevonQuerySummaryV1,
    qualification_audit,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryState,
    DiscoveryWindow,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
    SourceReference,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import PILOT_LIVE_STATUS, AuthorityRegistry
from yimby.store import EvidenceRegistrationAudit, SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("devon")
_RECEIPT_NAME = "devon-qualification-v4.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_EXACT_WINDOW_REQUIRED = "exact-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_QUALIFICATION_DAYS = 30
_EXPECTED_QUERY_COUNT = 6

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
    """Initial collection and immediate terminal-rerun costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class PendingWeeklyCycle(FrozenModel):
    """One later operational cycle that has not yet occurred."""

    sequence: Literal[1, 2]
    due_on: date
    status: Literal["pending"] = "pending"


class DevonQualificationReceiptV4(FrozenModel):
    """Versioned result of a complete local live qualification."""

    schema_version: Literal[4] = 4
    authority_id: Literal["devon"] = "devon"
    created_at: datetime
    scope: QualificationScope
    expected_queries: tuple[str, ...]
    completed_queries: tuple[str, ...]
    query_summaries: tuple[DevonQuerySummaryV1, ...]
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    weekly_cycles: tuple[PendingWeeklyCycle, PendingWeeklyCycle]
    operational_status: Literal["pending-weekly-cycles"] = "pending-weekly-cycles"
    registry_promotion: Literal["not-performed"] = "not-performed"


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
        description="Persist and qualify Devon live collection.",
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
    if (end - start).days + 1 != _QUALIFICATION_DAYS:
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


def _checkpoint_audit(
    store: SqliteStore,
    scope: QualificationScope,
) -> DevonQualificationAuditV1 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = DevonCheckpointV1.model_validate_json(stored.payload_json)
        return qualification_audit(
            checkpoint,
            DiscoveryWindow(
                start=scope.start,
                end=scope.end,
                include_open=scope.include_open,
            ),
        )
    except ValueError:
        return None


def _application_identities(store: SqliteStore) -> tuple[SourceReference, ...]:
    return store.application_identities(_AUTHORITY_ID)


def _reference_agreement(
    audit: DevonQualificationAuditV1 | None,
    state: DiscoveryState,
    applications: tuple[SourceReference, ...],
) -> bool:
    if audit is None or not audit.references:
        return False
    audited = {
        (item.source_id, item.reference, item.locator) for item in audit.references
    }
    durable = {(item.source_id, item.reference, item.locator) for item in state.queued}
    persisted = {
        (item.source_id, item.reference, item.locator) for item in applications
    }
    return (
        len(audited) == len(audit.references)
        and audited == durable
        and audited == persisted
        and len(applications) == len(persisted)
    )


def _discovery_evidence_complete(
    receipt_audit: DevonQualificationAuditV1 | None,
    evidence_audit: EvidenceRegistrationAudit,
) -> bool:
    if receipt_audit is None or not evidence_audit.discovery_registrations:
        return False
    retained_pages = {
        (item.query_key, item.page) for item in evidence_audit.discovery_registrations
    }
    expected_pages = {
        (summary.query_key, page)
        for summary in receipt_audit.query_summaries
        for page in range(1, summary.page_count + 1)
    }
    return retained_pages == expected_pages


def _evidence_inventory(data_dir: Path) -> tuple[str, ...]:
    evidence_root = data_dir / "evidence"
    if not evidence_root.exists():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(evidence_root))
            for path in evidence_root.rglob("*")
            if path.is_file()
        )
    )


def _evidence_integrity(
    data_dir: Path,
    audit: EvidenceRegistrationAudit,
    application_count: int,
) -> bool:
    inventory = _evidence_inventory(data_dir)
    registrations = audit.registrations
    database_objects = audit.database_objects
    database_paths = tuple(item.path for item in database_objects)
    registered_pairs = tuple(
        (item.observation_id, item.digest) for item in registrations
    )
    if (
        not inventory
        or audit.application_count != application_count
        or audit.applications_with_evidence != application_count
        or audit.observation_count < application_count
        or audit.observations_with_evidence != audit.observation_count
        or audit.missing_digests
        or audit.unlinked_digests
        or not audit.current_rebuild_coherent
        or len(registered_pairs) != len(set(registered_pairs))
        or set(database_paths) != set(inventory)
    ):
        return False
    evidence_root = data_dir / "evidence"
    for evidence in database_objects:
        digest = str(evidence.digest)
        expected_path = f"{digest[:2]}/{digest}.gz"
        if evidence.path != expected_path:
            return False
        path = evidence_root / evidence.path
        try:
            body = gzip.decompress(path.read_bytes())
        except (OSError, EOFError):
            return False
        if sha256(body).hexdigest() != digest:
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
    data_dir: Path,
) -> tuple[QualificationCheck, ...]:
    audit = _checkpoint_audit(store, scope)
    state = store.discovery_state(_AUTHORITY_ID)
    applications = _application_identities(store)
    evidence_audit = store.evidence_registration_audit(_AUTHORITY_ID)
    return (
        QualificationCheck(
            name="terminal-checkpoint-coherence",
            ok=audit is not None and audit.terminal_coherent,
        ),
        QualificationCheck(
            name="exact-query-inventory",
            ok=(
                audit is not None
                and audit.expected_queries == audit.completed_queries
                and len(audit.expected_queries) == _EXPECTED_QUERY_COUNT
            ),
        ),
        QualificationCheck(
            name="durable-reference-application-agreement",
            ok=_reference_agreement(audit, state, applications),
        ),
        QualificationCheck(
            name="pending-retries",
            ok=snapshot.pending_retries == 0,
        ),
        QualificationCheck(
            name="failed-current-sections",
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
            name="evidence-integrity",
            ok=_evidence_integrity(
                data_dir,
                evidence_audit,
                snapshot.applications,
            ),
        ),
        QualificationCheck(
            name="discovery-evidence",
            ok=_discovery_evidence_complete(audit, evidence_audit),
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
) -> DevonQualificationReceiptV4:
    registry = AuthorityRegistry((DEVON_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    prior_status_count = len(store.run_statuses())
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    first_state = store.discovery_state(_AUTHORITY_ID)
    first_applications = _application_identities(store)
    first_evidence = _evidence_inventory(config.data_dir)
    initial_checks = _base_checks(
        store,
        first_snapshot,
        config.scope,
        initial,
        config.data_dir,
    )
    _require(initial_checks)

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    final_state = store.discovery_state(_AUTHORITY_ID)
    final_applications = _application_identities(store)
    final_evidence = _evidence_inventory(config.data_dir)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_checks = (
        *_base_checks(store, final_snapshot, config.scope, initial, config.data_dir),
        QualificationCheck(
            name="idempotent-rerun",
            ok=(
                first_snapshot == final_snapshot
                and first_state == final_state
                and first_applications == final_applications
                and first_evidence == final_evidence
            ),
        ),
        QualificationCheck(
            name="terminal-rerun-network-io",
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
    audit = cast("DevonQualificationAuditV1", _checkpoint_audit(store, config.scope))
    return DevonQualificationReceiptV4(
        created_at=now(),
        scope=config.scope,
        expected_queries=audit.expected_queries,
        completed_queries=audit.completed_queries,
        query_summaries=audit.query_summaries,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        weekly_cycles=(
            PendingWeeklyCycle(sequence=1, due_on=config.scope.end + timedelta(days=7)),
            PendingWeeklyCycle(
                sequence=2, due_on=config.scope.end + timedelta(days=14)
            ),
        ),
    )


def _write_receipt(path: Path, receipt: DevonQualificationReceiptV4) -> None:
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


def _preserved_receipt(
    store: SqliteStore,
    config: _Config,
    path: Path,
) -> DevonQualificationReceiptV4 | None:
    """Require original live transport proof whenever durable history exists."""
    has_history = bool(
        store.run_statuses()
        or store.discovery_state(_AUTHORITY_ID).checkpoint is not None
        or _application_identities(store)
    )
    if not has_history:
        return None
    try:
        prior = DevonQualificationReceiptV4.model_validate_json(path.read_text())
    except (OSError, ValueError) as error:
        raise QualificationFailedError(("preserved-live-receipt",)) from error
    if (
        prior.scope != config.scope
        or prior.costs.initial.request_count == 0
        or prior.costs.rerun.request_count != 0
        or prior.costs.rerun.transferred_bytes != 0
        or prior.costs.rerun.attachment_body_requests != 0
        or not all(check.ok for check in prior.checks)
    ):
        raise QualificationFailedError(("preserved-live-receipt",))
    return prior


def _receipt_to_persist(
    prior: DevonQualificationReceiptV4 | None,
    candidate: DevonQualificationReceiptV4,
) -> DevonQualificationReceiptV4:
    if candidate.costs.initial.request_count != 0:
        if prior is not None:
            raise QualificationFailedError(("preserved-live-receipt",))
        return candidate
    if prior is not None and (
        prior.scope == candidate.scope
        and prior.expected_queries == candidate.expected_queries
        and prior.completed_queries == candidate.completed_queries
        and prior.query_summaries == candidate.query_summaries
        and prior.counts == candidate.counts
        and prior.run_statuses == candidate.run_statuses
        and prior.checks == candidate.checks
        and prior.weekly_cycles == candidate.weekly_cycles
        and prior.costs.initial.request_count > 0
        and prior.costs.rerun.request_count == 0
        and prior.costs.rerun.transferred_bytes == 0
        and prior.costs.rerun.attachment_body_requests == 0
    ):
        return prior
    raise QualificationFailedError(("preserved-live-receipt",))


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
                receipt_path = config.data_dir / _RECEIPT_NAME
                prior = _preserved_receipt(store, config, receipt_path)
                candidate = asyncio.run(_qualify(store, config, session_factory, now))
                receipt = _receipt_to_persist(prior, candidate)
                if prior is None:
                    _write_receipt(receipt_path, receipt)
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
