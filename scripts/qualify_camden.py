# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist and prove Camden's official live bootstrap without attachments."""

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
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from yimby.authorities.camden import CAMDEN_PACKAGE
from yimby.authorities.camden.browser_session import CamdenBrowserPortalSession
from yimby.authorities.camden.discovery import (
    CAMDEN_SOURCE,
    CamdenCheckpointV1,
    CamdenDiscoveryQueryV1,
    CamdenDiscoveryScopeV1,
    CamdenLiveCheckpointV1,
    CamdenSeenReferenceV1,
    CamdenTerminalV1,
    camden_query_inventory,
)
from yimby.collection import Collector
from yimby.domain import (
    ApplicationId,
    AuthorityId,
    DiscoveryWindow,
    EvidenceIntegrityReport,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
    SourceReference,
)
from yimby.evidence import EvidenceStore
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("camden")
_RECEIPT_NAME = "camden-qualification-v2.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_WINDOW_REQUIRED = "30-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_RESUME_SCOPE_MISMATCH = "resume-scope-mismatch"
_WINDOW_SPAN_DAYS = 29
_QUERY_COUNT = 5

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]
SectionVerifier = Callable[[SqliteStore], bool]


class QualificationScope(FrozenModel):
    """Exact inclusive live scope proven by the receipt."""

    start: date
    end: date
    include_open: Literal[True] = True


class QualificationCounts(FrozenModel):
    """Durable authority counts after qualification."""

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
    """Observable transport cost for one pass."""

    attempted_request_count: int = Field(ge=0)
    successful_capture_count: int = Field(ge=0)
    retained_html_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    """Initial bootstrap and immediate terminal rerun costs."""

    initial: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class CamdenQueryResultProofV1(FrozenModel):
    """Inspectible membership proof for one source query."""

    query: CamdenDiscoveryQueryV1
    reported_count: int = Field(ge=0)
    enumerated_count: int = Field(ge=0)
    membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CamdenReferenceAgreementV1(FrozenModel):
    """Direct equality and hashes for every durable identity view."""

    checkpoint_count: int = Field(ge=0)
    discovery_count: int = Field(ge=0)
    application_count: int = Field(ge=0)
    rebuild_input_count: int = Field(ge=0)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovery_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rebuild_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_match: bool


class CamdenWeeklyRefreshV1(FrozenModel):
    """One required later operational cycle not claimed by bootstrap."""

    ordinal: Literal[1, 2]
    due_on: date
    status: Literal["pending"] = "pending"


class CamdenQualificationReceiptV2(FrozenModel):
    """Versioned proof of Camden's complete same-day live qualification."""

    schema_version: Literal[2] = 2
    authority_id: Literal["camden"] = "camden"
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[CamdenDiscoveryQueryV1, ...]
    query_results: tuple[CamdenQueryResultProofV1, ...]
    reference_agreement: CamdenReferenceAgreementV1
    evidence_integrity: EvidenceIntegrityReport
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    weekly_refresh_cycles: tuple[CamdenWeeklyRefreshV1, CamdenWeeklyRefreshV1]
    checks: tuple[QualificationCheck, ...]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope
    resume: bool


class _QualificationState(FrozenModel):
    snapshot: QualificationSnapshot
    checkpoint: CamdenLiveCheckpointV1 | None
    agreement: CamdenReferenceAgreementV1
    evidence: EvidenceIntegrityReport
    initial: QualificationCost


class _QualificationPass(FrozenModel):
    cost: QualificationCost
    applications: tuple[ApplicationId, ...]


class QualificationConfigError(ValueError):
    """One required safety option or scope value is invalid."""


@runtime_checkable
class _MeasuredCamdenSession(Protocol):
    @property
    def attempted_request_count(self) -> int: ...

    @property
    def successful_capture_count(self) -> int: ...

    @property
    def retained_html_bytes(self) -> int: ...

    def __init__(self, code: str) -> None:
        """Retain the stable error code emitted by the command."""
        super().__init__(code)


class QualificationFailedError(RuntimeError):
    """One or more acceptance invariants failed."""

    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        """Retain the stable check names for safe stderr output."""
        super().__init__("qualification checks failed")
        self.failed_checks = failed_checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist and qualify Camden live data."
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
    if (end - start).days != _WINDOW_SPAN_DAYS:
        raise QualificationConfigError(_WINDOW_REQUIRED)
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(
        data_dir=data_dir,
        scope=QualificationScope(start=start, end=end, include_open=True),
        resume=bool(arguments.resume),
    )


async def _collect_once(
    collector: Collector,
    window: DiscoveryWindow,
    session_factory: SessionFactory,
) -> _QualificationPass:
    session = session_factory()
    try:
        report = await collector.collect(_AUTHORITY_ID, window, session)
        if isinstance(session, _MeasuredCamdenSession):
            attempted_request_count = session.attempted_request_count
            successful_capture_count = session.successful_capture_count
            retained_html_bytes = session.retained_html_bytes
        else:
            attempted_request_count = len(report.requested_urls)
            successful_capture_count = len(report.requested_urls)
            retained_html_bytes = session.transferred_bytes
        return _QualificationPass(
            cost=QualificationCost(
                attempted_request_count=attempted_request_count,
                successful_capture_count=successful_capture_count,
                retained_html_bytes=retained_html_bytes,
                attachment_body_requests=report.attachment_body_requests,
            ),
            applications=report.applications,
        )
    finally:
        await session.aclose()


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> CamdenLiveCheckpointV1 | None:
    stored = store.discovery_state(_AUTHORITY_ID).checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = CamdenCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None
    live = checkpoint.root
    expected_scope = CamdenDiscoveryScopeV1(
        start=scope.start,
        end=scope.end,
        include_open=True,
    )
    if (
        not isinstance(live, CamdenLiveCheckpointV1)
        or live.scope != expected_scope
        or live.query_inventory != camden_query_inventory(expected_scope)
        or not isinstance(live.progress, CamdenTerminalV1)
        or not live.seen_references
    ):
        return None
    return live


def _query_results(
    checkpoint: CamdenLiveCheckpointV1,
) -> tuple[CamdenQueryResultProofV1, ...]:
    return tuple(
        CamdenQueryResultProofV1(
            query=result.query,
            reported_count=result.reported_count,
            enumerated_count=len(result.ordered_references),
            membership_sha256=_seen_hash(result.ordered_references),
        )
        for result in checkpoint.completed_queries
    )


def _reference_agreement(
    store: SqliteStore,
    checkpoint: CamdenLiveCheckpointV1 | None,
) -> CamdenReferenceAgreementV1:
    stored = store.authority_reference_sets(_AUTHORITY_ID)
    checkpoint_references = (
        ()
        if checkpoint is None
        else tuple(
            SourceReference(
                source_id=CAMDEN_SOURCE,
                reference=item.reference,
                locator=item.locator,
            )
            for item in checkpoint.seen_references
        )
    )
    checkpoint_set = _located_set(checkpoint_references)
    discovery_set = _located_set(stored.discovery)
    application_set = _located_set(stored.applications)
    rebuild_set = _located_set(stored.rebuild_inputs)
    discovery_identities = _identity_set(stored.discovery)
    exact_match = (
        bool(checkpoint_set)
        and checkpoint_set == discovery_set
        and discovery_identities == _identity_set(stored.applications)
        and discovery_identities == _identity_set(stored.rebuild_inputs)
        and application_set == rebuild_set
    )
    return CamdenReferenceAgreementV1(
        checkpoint_count=len(checkpoint_set),
        discovery_count=len(discovery_set),
        application_count=len(application_set),
        rebuild_input_count=len(rebuild_set),
        checkpoint_sha256=_reference_hash(checkpoint_set),
        discovery_sha256=_reference_hash(discovery_set),
        application_sha256=_reference_hash(application_set),
        rebuild_input_sha256=_reference_hash(rebuild_set),
        exact_match=exact_match,
    )


def _seen_hash(references: tuple[CamdenSeenReferenceV1, ...]) -> str:
    values = tuple(sorted((item.reference, item.locator) for item in references))
    payload = json.dumps(values, separators=(",", ":")).encode()
    return sha256(payload).hexdigest()


def _located_set(references: tuple[SourceReference, ...]) -> set[tuple[str, str, str]]:
    return {
        (str(item.source_id), item.reference, item.locator or "") for item in references
    }


def _identity_set(references: tuple[SourceReference, ...]) -> set[tuple[str, str]]:
    return {(str(item.source_id), item.reference) for item in references}


def _reference_hash(references: set[tuple[str, str, str]]) -> str:
    payload = json.dumps(sorted(references), separators=(",", ":")).encode()
    return sha256(payload).hexdigest()


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _base_checks(
    store: SqliteStore,
    state: _QualificationState,
    *,
    exposed_child_sections_verified: bool,
) -> tuple[QualificationCheck, ...]:
    completeness = store.authority_completeness(_AUTHORITY_ID)
    complete_kinds = {"complete", "empty"}
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=state.checkpoint is not None,
        ),
        QualificationCheck(
            name="query-inventory",
            ok=(
                state.checkpoint is not None
                and len(state.checkpoint.completed_queries) == _QUERY_COUNT
            ),
        ),
        QualificationCheck(
            name="reference-agreement",
            ok=state.agreement.exact_match,
        ),
        QualificationCheck(
            name="application-count",
            ok=state.snapshot.applications > 0,
        ),
        QualificationCheck(
            name="pending-retries",
            ok=state.snapshot.pending_retries == 0,
        ),
        QualificationCheck(
            name="failed-sections",
            ok=state.snapshot.failed_sections == 0,
        ),
        QualificationCheck(
            name="required-sections-complete",
            ok=(
                len(completeness) == state.snapshot.applications
                and all(
                    section.kind in complete_kinds
                    for item in completeness
                    for section in (
                        item.application,
                        item.documents,
                        item.comments,
                    )
                )
            ),
        ),
        QualificationCheck(
            name="exposed-child-sections-verified",
            ok=exposed_child_sections_verified,
        ),
        QualificationCheck(
            name="unmapped-records",
            ok=state.snapshot.unmapped_records == 0,
        ),
        QualificationCheck(
            name="database-integrity",
            ok=store.database_integrity() == "ok",
        ),
        QualificationCheck(
            name="evidence-integrity",
            ok=state.evidence.captures_checked > 0 and not state.evidence.issues,
        ),
        QualificationCheck(
            name="attachment-policy",
            ok=state.initial.attachment_body_requests == 0,
        ),
    )


def _require(checks: tuple[QualificationCheck, ...]) -> None:
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise QualificationFailedError(failed)


def _require_checkpoint(
    checkpoint: CamdenLiveCheckpointV1 | None,
) -> CamdenLiveCheckpointV1:
    if checkpoint is None:
        raise QualificationFailedError(("terminal-checkpoint",))
    return checkpoint


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
    section_verifier: SectionVerifier,
) -> CamdenQualificationReceiptV2:
    collector = Collector(AuthorityRegistry((CAMDEN_PACKAGE,)), store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    prior_status_count = len(store.run_statuses())
    initial_pass = await _collect_once(collector, window, session_factory)
    initial = initial_pass.cost
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    first_checkpoint = _terminal_checkpoint(store, config.scope)
    first_agreement = _reference_agreement(store, first_checkpoint)
    first_evidence = store.evidence_integrity(_AUTHORITY_ID)
    first_state = _QualificationState(
        snapshot=first_snapshot,
        checkpoint=first_checkpoint,
        agreement=first_agreement,
        evidence=first_evidence,
        initial=initial,
    )
    exposed_child_sections_verified = section_verifier(store)
    initial_checks = _base_checks(
        store,
        first_state,
        exposed_child_sections_verified=exposed_child_sections_verified,
    )
    _require(initial_checks)
    first_checkpoint = _require_checkpoint(first_checkpoint)
    first_query_results = _query_results(first_checkpoint)

    expected_refreshes = tuple(
        view.application.id
        for view in store.application_views()
        if view.application.authority_id == _AUTHORITY_ID
    )
    refresh_due = datetime.min.replace(tzinfo=UTC)
    for application_id in expected_refreshes:
        store.set_refresh_schedule(
            application_id,
            refresh_due,
            "immediate",
            "same-day-qualification-refresh",
        )
    rerun_pass = await _collect_once(collector, window, session_factory)
    rerun = rerun_pass.cost
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    final_checkpoint = _terminal_checkpoint(store, config.scope)
    final_agreement = _reference_agreement(store, final_checkpoint)
    final_evidence = store.evidence_integrity(_AUTHORITY_ID)
    run_statuses = store.run_statuses()[prior_status_count:]
    final_state = _QualificationState(
        snapshot=final_snapshot,
        checkpoint=final_checkpoint,
        agreement=final_agreement,
        evidence=final_evidence,
        initial=initial,
    )
    final_checks = (
        *_base_checks(
            store,
            final_state,
            exposed_child_sections_verified=exposed_child_sections_verified,
        ),
        QualificationCheck(
            name="idempotent-rerun",
            ok=(
                first_snapshot == final_snapshot
                and first_agreement == final_agreement
                and first_evidence == final_evidence
                and final_checkpoint is not None
                and first_query_results == _query_results(final_checkpoint)
            ),
        ),
        QualificationCheck(
            name="discovery-evidence-retained",
            ok=store.discovery_evidence_count(_AUTHORITY_ID) > 0,
        ),
        QualificationCheck(
            name="immediate-refresh",
            ok=(
                bool(expected_refreshes)
                and set(rerun_pass.applications) == set(expected_refreshes)
                and len(rerun_pass.applications) == len(expected_refreshes)
                and rerun.attempted_request_count > 0
                and rerun.successful_capture_count > 0
                and rerun.attachment_body_requests == 0
            ),
        ),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    final_checkpoint = _require_checkpoint(final_checkpoint)
    return CamdenQualificationReceiptV2(
        created_at=now(),
        scope=config.scope,
        query_inventory=final_checkpoint.query_inventory,
        query_results=_query_results(final_checkpoint),
        reference_agreement=final_agreement,
        evidence_integrity=final_evidence,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=initial, rerun=rerun),
        run_statuses=run_statuses,
        weekly_refresh_cycles=(
            CamdenWeeklyRefreshV1(
                ordinal=1,
                due_on=config.scope.end + timedelta(days=7),
            ),
            CamdenWeeklyRefreshV1(
                ordinal=2,
                due_on=config.scope.end + timedelta(days=14),
            ),
        ),
        checks=final_checks,
    )


def _resume_scope_matches(store: SqliteStore, scope: QualificationScope) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    if state.checkpoint is None:
        return True
    try:
        checkpoint = CamdenCheckpointV1.model_validate_json(
            state.checkpoint.payload_json
        )
    except ValueError:
        return False
    live = checkpoint.root
    return isinstance(live, CamdenLiveCheckpointV1) and live.scope == (
        CamdenDiscoveryScopeV1(
            start=scope.start,
            end=scope.end,
            include_open=True,
        )
    )


def _write_receipt(path: Path, receipt: CamdenQualificationReceiptV2) -> None:
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


def _default_session() -> CamdenBrowserPortalSession:
    return CamdenBrowserPortalSession()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _unverified_child_sections(_store: SqliteStore) -> bool:
    return False


def _error(code: str, exit_code: int, **details: object) -> int:
    print(json.dumps({"error": code, **details}, sort_keys=True), file=sys.stderr)
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory = _default_session,
    now: Clock = _default_clock,
    section_verifier: SectionVerifier = _unverified_child_sections,
) -> int:
    """Run explicit live qualification and emit its atomic proof receipt."""
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
                if config.resume and not _resume_scope_matches(store, config.scope):
                    raise QualificationConfigError(_RESUME_SCOPE_MISMATCH)
                receipt = asyncio.run(
                    _qualify(
                        store,
                        config,
                        session_factory,
                        now,
                        section_verifier,
                    )
                )
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
