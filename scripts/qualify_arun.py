# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Qualify Arun live collection without changing registry readiness."""

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
from typing import Literal

from pydantic import Field, ValidationError

from yimby.authorities.arun import ARUN_PACKAGE
from yimby.authorities.arun.adapter import (
    ArunApplicationV1,
    ArunCheckpointError,
    ArunCheckpointV1,
    ArunComplete,
    ArunCompletedQuery,
    ArunCountMismatchError,
    ArunDiscoveryScope,
    ArunLiveCursor,
    ArunParseError,
    ArunQuery,
    ArunQueryReplayError,
    ArunReferenceMismatchError,
    ArunRequestContract,
    ArunResultCapError,
    ArunRoutingError,
    ArunSearchForm,
    _canonical_query_plan,
    _initial_search_request,
    _parse_application_pages,
    _parse_search_form,
    _parse_search_results,
    _request_evidence,
    _show_all_request,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    FrozenModel,
    LiveReadiness,
    QualificationSnapshot,
    RetainedNativeRecord,
    RunStatus,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.orchestration import CollectionAlreadyRunningError, ProcessLock
from yimby.registry import PILOT_LIVE_STATUS, AuthorityRegistry
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalSession,
    SourceUnavailableError,
)

_AUTHORITY_ID = AuthorityId("arun")
_RECEIPT_NAME = "arun-qualification-v3.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_THIRTY_DAY_WINDOW_REQUIRED = "thirty-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_INCLUSIVE_WINDOW_SPAN_DAYS = 29
_EVIDENCE_PER_APPLICATION = 2
_REQUIRED_SUCCESSFUL_RUNS = 2

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
    source_reported_count: int | None = Field(default=None, ge=0, lt=200)
    enumerated_count: int = Field(ge=0, lt=200)
    references: tuple[str, ...]
    initial_evidence_digest: EvidenceDigest
    expanded_evidence_digest: EvidenceDigest | None = None
    initial_request: ArunRequestContract | None = None
    expanded_request: ArunRequestContract | None = None


class QualificationReferences(FrozenModel):
    """Exact durable reference sets compared during qualification."""

    discovered: tuple[str, ...]
    retained_native: tuple[str, ...]
    applications: tuple[str, ...]


class QualificationEvidence(FrozenModel):
    """Recomputed evidence inventory retained for the qualified records."""

    application_capture_count: int = Field(ge=0)
    application_digests: tuple[EvidenceDigest, ...]
    search_capture_count: int = Field(ge=0)
    search_digests: tuple[EvidenceDigest, ...]


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


class QualificationNativeCoverage(FrozenModel):
    """Evidence-reconciled coverage of Arun-native appeal fields."""

    applications: int = Field(ge=0)
    appeal_references: int = Field(ge=0)
    appeal_statuses: int = Field(ge=0)
    appeal_lodged_dates: int = Field(ge=0)
    appeal_decision_dates: int = Field(ge=0)


class QualificationCost(FrozenModel):
    """Observable transport cost for one qualification pass."""

    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class QualificationCosts(FrozenModel):
    """Cumulative bootstrap, final attempt, and terminal-rerun costs."""

    bootstrap_total: QualificationCost
    final_resume_attempt: QualificationCost
    rerun: QualificationCost


class QualificationCheck(FrozenModel):
    """One named acceptance invariant."""

    name: str
    ok: bool


class WeeklyCycle(FrozenModel):
    """A later operational qualification cycle not yet run."""

    target_date: date
    status: Literal["pending"] = "pending"


class ArunQualificationReceiptV3(FrozenModel):
    """Versioned result of a complete local Arun bootstrap qualification."""

    schema_version: Literal[3] = 3
    authority_id: Literal["arun"] = "arun"
    created_at: datetime
    scope: QualificationScope
    bootstrap_status: Literal["proved"] = "proved"
    operational_status: Literal["pending-weekly-refreshes"] = "pending-weekly-refreshes"
    registry_readiness: Literal["discovery-only"] = "discovery-only"
    search_form_evidence_digest: EvidenceDigest
    query_inventory: tuple[QualificationQuery, ...]
    references: QualificationReferences
    evidence: QualificationEvidence
    counts: QualificationCounts
    native_coverage: QualificationNativeCoverage | None = None
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
    search_form_evidence_digest: EvidenceDigest | None
    references: QualificationReferences
    evidence: QualificationEvidence
    semantic_fingerprint: str
    terminal_checkpoint: bool
    reference_sets_agree: bool
    evidence_integrity: bool
    search_evidence_integrity: bool
    current_sections_complete: bool
    native_evidence_agreement: bool
    normalised_evidence_agreement: bool
    native_coverage: QualificationNativeCoverage


class _TerminalDiscoveryProof(FrozenModel):
    """Revalidated terminal query inventory and retained search evidence."""

    inventory: tuple[QualificationQuery, ...]
    search_form_evidence_digest: EvidenceDigest
    search_digests: tuple[EvidenceDigest, ...]


class _ValidatedQueryEvidence(FrozenModel):
    """One completed query reparsed from its retained response bodies."""

    inventory: QualificationQuery
    digests: tuple[EvidenceDigest, ...]


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


class QualificationRuntimeError(RuntimeError):
    """An expected source, evidence, storage, or filesystem operation failed."""

    def __init__(self, error: Exception) -> None:
        """Retain only the public exception kind and stable Arun parser code."""
        super().__init__(type(error).__name__)
        self.exception_name = type(error).__name__
        self.source_error = error.code if isinstance(error, ArunParseError) else None


_EXPECTED_RUNTIME_ERRORS = (
    ArunCheckpointError,
    ArunCountMismatchError,
    ArunParseError,
    ArunQueryReplayError,
    ArunReferenceMismatchError,
    ArunResultCapError,
    ArunRoutingError,
    AttachmentBodyBlockedError,
    CollectionAlreadyRunningError,
    OSError,
    SourceUnavailableError,
    sqlite3.Error,
    ValidationError,
)


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


def _terminal_inventory(  # noqa: PLR0911
    store: SqliteStore,
    scope: QualificationScope,
) -> _TerminalDiscoveryProof | None:
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
        or cursor.search_form_evidence is None
        or set(cursor.progress.seen_references) != set(state.references)
        or len(cursor.progress.seen_references) != len(state.references)
    ):
        return None
    try:
        form_capture = store.evidence_capture(cursor.search_form_evidence)
        if form_capture is None or not _capture_is_valid(form_capture):
            return None
        form = _parse_search_form(form_capture.body)
        inventory = []
        search_digests = [cursor.search_form_evidence]
        for query, completed in zip(
            cursor.plan,
            cursor.progress.completed,
            strict=True,
        ):
            validated = _validate_query_evidence(store, form, query, completed)
            if validated is None:
                return None
            inventory.append(validated.inventory)
            search_digests.extend(validated.digests)
    except (OSError, ValueError):
        return None
    return _TerminalDiscoveryProof(
        inventory=tuple(inventory),
        search_form_evidence_digest=cursor.search_form_evidence,
        search_digests=tuple(search_digests),
    )


def _capture_is_valid(capture: EvidenceCapture) -> bool:
    return sha256(capture.body).hexdigest() == str(capture.digest)


def _validate_query_evidence(  # noqa: PLR0911
    store: SqliteStore,
    form: ArunSearchForm,
    query: ArunQuery,
    completed: ArunCompletedQuery,
) -> _ValidatedQueryEvidence | None:
    initial = store.evidence_capture(completed.initial_evidence)
    if initial is None or not _capture_is_valid(initial):
        return None
    parsed_initial = _parse_search_results(initial.body)
    expected_initial_request = _request_evidence(_initial_search_request(form, query))
    if (
        parsed_initial.reported != completed.reported_count
        or completed.initial_request not in (None, expected_initial_request)
    ):
        return None
    digests = [completed.initial_evidence]
    parsed_final = parsed_initial
    if completed.expanded_evidence is None:
        if parsed_initial.has_show_all or completed.expanded_request is not None:
            return None
    else:
        if not parsed_initial.has_show_all:
            return None
        expanded = store.evidence_capture(completed.expanded_evidence)
        if expanded is None or not _capture_is_valid(expanded):
            return None
        parsed_final = _parse_search_results(expanded.body)
        digests.append(completed.expanded_evidence)
        if (
            parsed_initial.reported is None
            or parsed_final.has_show_all
            or parsed_final.reported not in (None, parsed_initial.reported)
            or completed.expanded_request
            not in (
                None,
                _request_evidence(
                    _show_all_request(parsed_initial.show_all_form, query)
                ),
            )
        ):
            return None
    final_references = tuple(
        reference.reference for reference in parsed_final.references
    )
    if (
        final_references != completed.references
        or len(final_references) != completed.enumerated_count
    ):
        return None
    return _ValidatedQueryEvidence(
        inventory=QualificationQuery(
            query=query,
            source_reported_count=parsed_initial.reported,
            enumerated_count=len(final_references),
            references=final_references,
            initial_evidence_digest=completed.initial_evidence,
            expanded_evidence_digest=completed.expanded_evidence,
            initial_request=expected_initial_request,
            expanded_request=(
                None
                if completed.expanded_evidence is None
                else _request_evidence(
                    _show_all_request(parsed_initial.show_all_form, query)
                )
            ),
        ),
        digests=tuple(digests),
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
) -> tuple[
    int,
    tuple[EvidenceDigest, ...],
    bool,
    bool,
    bool,
    bool,
    QualificationNativeCoverage,
]:
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
    native_evidence_agreement = True
    normalised_evidence_agreement = True
    native_rows = []
    for record in retained:
        native = ArunApplicationV1.model_validate_json(record.native_json)
        native_rows.append(native)
        current = store.get_application(record.application_id)
        native_evidence_agreement = (
            native_evidence_agreement and _native_evidence_agrees(record, native)
        )
        normalised_evidence_agreement = (
            normalised_evidence_agreement and _normalised_evidence_agrees(store, record)
        )
        sections_complete = sections_complete and (
            record.completeness.application.kind == "complete"
            and record.completeness.documents.kind in {"complete", "empty"}
            and current.completeness.application.kind == "complete"
            and current.completeness.documents.kind in {"complete", "empty"}
            and len(record.evidence) == _EVIDENCE_PER_APPLICATION
            and all(document.source_links for document in native.documents)
        )
    return (
        len(captures),
        tuple(sorted(capture.digest for capture in captures)),
        integrity,
        sections_complete,
        native_evidence_agreement,
        normalised_evidence_agreement,
        QualificationNativeCoverage(
            applications=len(native_rows),
            appeal_references=sum(
                item.appeal_reference is not None for item in native_rows
            ),
            appeal_statuses=sum(item.appeal_status is not None for item in native_rows),
            appeal_lodged_dates=sum(
                item.appeal_lodged_date is not None for item in native_rows
            ),
            appeal_decision_dates=sum(
                item.appeal_decision_date is not None for item in native_rows
            ),
        ),
    )


def _native_evidence_agrees(
    record: RetainedNativeRecord,
    native: ArunApplicationV1,
) -> bool:
    if len(record.evidence) != _EVIDENCE_PER_APPLICATION:
        return False
    try:
        expected = _parse_application_pages(
            record.evidence[0].body,
            record.evidence[1].body,
            record.reference.reference,
        )
    except ArunReferenceMismatchError:
        return False
    return native == expected


def _normalised_evidence_agrees(
    store: SqliteStore,
    record: RetainedNativeRecord,
) -> bool:
    expected = ARUN_PACKAGE.rebuild(record)
    view = store.application_view(record.application_id)
    metadata = expected.metadata.model_copy(
        update={
            "aliases": tuple(sorted(expected.metadata.aliases)),
            "published_parties": tuple(sorted(expected.metadata.published_parties)),
            "constraints": tuple(sorted(expected.metadata.constraints)),
            "conditions": tuple(sorted(expected.metadata.conditions)),
            "consultations": tuple(sorted(expected.metadata.consultations)),
            "events": tuple(
                sorted(
                    expected.metadata.events,
                    key=lambda event: (
                        event.event_at,
                        event.event_type,
                        event.details or "",
                    ),
                )
            ),
            "relationships": tuple(
                sorted(
                    expected.metadata.relationships,
                    key=lambda relationship: (
                        relationship.relationship_type,
                        relationship.related_reference,
                    ),
                )
            ),
        }
    )
    application = view.application
    return (
        application.id == record.application_id
        and application.authority_id == expected.authority_id
        and application.reference == expected.reference.reference
        and application.proposal == expected.proposal
        and application.status == expected.status
        and application.documents
        == tuple(
            sorted(
                expected.documents,
                key=lambda document: (document.title, str(document.url)),
            )
        )
        and application.comments
        == tuple(
            sorted(
                expected.comments,
                key=lambda comment: (comment.comment_id, comment.text),
            )
        )
        and application.completeness == expected.completeness
        and view.metadata == metadata
        and view.normaliser_version == expected.normaliser_version
        and view.observed_at == record.observed_at
        and not view.suppressed
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
    terminal = _terminal_inventory(store, scope)
    references = _references(store)
    (
        application_capture_count,
        application_digests,
        evidence_integrity,
        current_sections_complete,
        native_evidence_agreement,
        normalised_evidence_agreement,
        native_coverage,
    ) = _evidence_and_sections(store)
    search_digests = () if terminal is None else terminal.search_digests
    return _QualificationState(
        snapshot=snapshot,
        query_inventory=() if terminal is None else terminal.inventory,
        search_form_evidence_digest=(
            None if terminal is None else terminal.search_form_evidence_digest
        ),
        references=references,
        evidence=QualificationEvidence(
            application_capture_count=application_capture_count,
            application_digests=application_digests,
            search_capture_count=len(search_digests),
            search_digests=search_digests,
        ),
        semantic_fingerprint=_fingerprint(store, snapshot),
        terminal_checkpoint=terminal is not None,
        reference_sets_agree=(
            bool(references.discovered)
            and references.discovered == references.retained_native
            and references.discovered == references.applications
        ),
        evidence_integrity=evidence_integrity,
        search_evidence_integrity=terminal is not None,
        current_sections_complete=current_sections_complete,
        native_evidence_agreement=native_evidence_agreement,
        normalised_evidence_agreement=normalised_evidence_agreement,
        native_coverage=native_coverage,
    )


def _base_checks(
    store: SqliteStore,
    state: _QualificationState,
    scope: QualificationScope,
    bootstrap_cost: QualificationCost,
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
            ok=bootstrap_cost.attachment_body_requests == 0,
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
            name="application-evidence-digests",
            ok=state.evidence_integrity,
        ),
        QualificationCheck(
            name="search-evidence-digests",
            ok=state.search_evidence_integrity,
        ),
        QualificationCheck(
            name="application-evidence-capture-count",
            ok=(
                state.evidence.application_capture_count == snapshot.applications * 2
                and snapshot.applications > 0
            ),
        ),
        QualificationCheck(
            name="search-evidence-capture-count",
            ok=(state.evidence.search_capture_count >= len(state.query_inventory) + 1),
        ),
        QualificationCheck(
            name="current-sections",
            ok=state.current_sections_complete,
        ),
        QualificationCheck(
            name="native-evidence-agreement",
            ok=state.native_evidence_agreement,
        ),
        QualificationCheck(
            name="normalised-evidence-agreement",
            ok=state.normalised_evidence_agreement,
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
        QualificationCheck(
            name="durable-registry-status",
            ok=any(
                item.manifest.id == _AUTHORITY_ID
                and item.manifest.live_status
                == registry.manifest(_AUTHORITY_ID).live_status
                for item in store.authority_states()
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
) -> ArunQualificationReceiptV3:
    registry = AuthorityRegistry((ARUN_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    initial_cost = await _collect_once(collector, window, session_factory)
    totals = store.metrics_totals(_AUTHORITY_ID)
    bootstrap_cost = QualificationCost(
        request_count=totals.request_count,
        transferred_bytes=totals.transferred_bytes,
        attachment_body_requests=totals.attachment_body_requests,
    )
    initial_state = _state(store, config.scope)
    initial_checks = _base_checks(
        store,
        initial_state,
        config.scope,
        bootstrap_cost,
        registry,
    )
    _require(initial_checks)

    rerun_cost = await _collect_once(collector, window, session_factory)
    final_state = _state(store, config.scope)
    run_statuses = store.run_statuses(_AUTHORITY_ID)
    final_checks = (
        *_base_checks(
            store,
            final_state,
            config.scope,
            bootstrap_cost,
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
            ok=(
                len(run_statuses) >= _REQUIRED_SUCCESSFUL_RUNS
                and run_statuses[-_REQUIRED_SUCCESSFUL_RUNS:]
                == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED)
                and RunStatus.RUNNING not in run_statuses
            ),
        ),
    )
    _require(final_checks)
    if final_state.search_form_evidence_digest is None:
        raise QualificationFailedError(("search-form-evidence",))
    return ArunQualificationReceiptV3(
        created_at=now(),
        scope=config.scope,
        search_form_evidence_digest=final_state.search_form_evidence_digest,
        query_inventory=final_state.query_inventory,
        references=final_state.references,
        evidence=final_state.evidence,
        counts=_counts(final_state.snapshot),
        native_coverage=final_state.native_coverage,
        semantic_fingerprint=final_state.semantic_fingerprint,
        costs=QualificationCosts(
            bootstrap_total=bootstrap_cost,
            final_resume_attempt=initial_cost,
            rerun=rerun_cost,
        ),
        run_statuses=run_statuses,
        weekly_cycles=(
            WeeklyCycle(target_date=config.scope.end + timedelta(days=7)),
            WeeklyCycle(target_date=config.scope.end + timedelta(days=14)),
        ),
        checks=final_checks,
    )


def _write_receipt(path: Path, receipt: ArunQualificationReceiptV3) -> None:
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


def _run_qualification(
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> ArunQualificationReceiptV3:
    """Translate only expected operational boundary failures."""
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            store = SqliteStore(
                config.data_dir / "yimby.sqlite3",
                EvidenceStore(config.data_dir / "evidence"),
            )
            try:
                receipt = asyncio.run(_qualify(store, config, session_factory, now))
                _write_receipt(config.data_dir / _RECEIPT_NAME, receipt)
                return receipt
            finally:
                store.close()
    except _EXPECTED_RUNTIME_ERRORS as error:
        raise QualificationRuntimeError(error) from error


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
        receipt = _run_qualification(config, session_factory, now)
    except QualificationFailedError as error:
        return _error(
            "qualification-failed",
            1,
            failed_checks=list(error.failed_checks),
        )
    except QualificationRuntimeError as error:
        details: dict[str, object] = {"exception": error.exception_name}
        if error.source_error is not None:
            details["source_error"] = error.source_error
        return _error("runtime-failure", 1, **details)
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
