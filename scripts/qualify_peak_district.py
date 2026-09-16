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
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import ConfigDict, Field

from yimby.authorities.peak_district import PEAK_DISTRICT_PACKAGE
from yimby.authorities.peak_district.adapter import (
    _ADVANCED_FORM_URL,
    _SEARCH_URL,
    LEGACY_SOURCE,
    PeakDistrictCheckpointV1,
    PeakDistrictDiscoveryPageV1,
    PeakDistrictDiscoveryScope,
    PeakDistrictQueryV1,
    _pagination_request,
    _parse_search_form,
    _parse_search_page,
    _query_request,
    peak_district_evidence_key,
    peak_district_query_inventory,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    EvidenceCapture,
    FrozenModel,
    LiveReadiness,
    LiveTransportKind,
    QualificationSnapshot,
    RetainedNativeRecord,
    RunStatus,
    SourceReference,
)
from yimby.evidence import EvidenceIntegrityError, EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import PILOT_LIVE_STATUS, AuthorityRegistry
from yimby.store import (
    EvidenceRegistrationAudit,
    RetainedDiscoveryEvidenceRegistration,
    SqliteStore,
)
from yimby.transport import PortalRequest, PortalSession

_AUTHORITY_ID = AuthorityId("peak-district")
_RECEIPT_NAME = "peak-district-qualification-v1.json"
_PROOF_NAME = "peak-district-qualification-proof-v1.json"
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_THIRTY_DAYS_REQUIRED = "thirty-days-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_SCOPE_MISMATCH = "scope-mismatch"
_CYCLE_NOT_ELIGIBLE = "cycle-not-eligible"
_WEEKLY_CYCLES_COMPLETE = "weekly-cycles-complete"
_INCLUSIVE_WINDOW_DAYS = 30
_MINIMUM_QUALIFICATION_RUNS = 2
_SECOND_WEEKLY_CYCLE: Literal[2] = 2
_MINIMUM_APPLICATION_CAPTURES = 2
_COMMITMENT_CANONICALIZATION: Literal["sha256-canonical-json-v1"] = (
    "sha256-canonical-json-v1"
)
_DETAIL_PATH = (
    "/AssureLive/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview"
)
_DOCUMENTS_PATH = (
    "/AssureLive/ES/Presentation/Planning/OnlinePlanning/GetOnlineDocuments"
)
_ATTACHMENT_PATH = (
    "/AssureLive/ES/Presentation/Planning/OnlineDisplayDocument/DisplaySearchDocument/"
)
_CHECK_NAMES = (
    "terminal-checkpoint",
    "exact-query-inventory",
    "reference-application-agreement",
    "pending-retries",
    "retry-inventory",
    "failed-sections",
    "attachment-policy",
    "database-integrity",
    "authority-readiness",
    "evidence-paths",
    "evidence-integrity",
    "application-evidence",
    "application-count",
    "unmapped-records",
    "idempotent-rerun",
    "terminal-rerun-requests",
    "run-statuses",
)

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]
type WeeklyCycleNumber = Literal[1, 2]


class _ReceiptModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QualificationScope(_ReceiptModel):
    start: date
    end: date
    include_open: Literal[True] = True


class QualificationIdentity(_ReceiptModel):
    source_id: Literal["peak-district-legacy"] = "peak-district-legacy"
    reference: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class QualificationQuery(_ReceiptModel):
    kind: Literal["bounded-date", "older-open"]
    field: Literal[
        "Received",
        "Validated",
        "Decided",
        "AdvanceSearch.SelectedApplicationStatus",
    ]
    value: str = Field(min_length=1)


class QualificationCounts(_ReceiptModel):
    applications: int = Field(ge=0)
    discovered_references: int = Field(ge=0)
    native_versions: int = Field(ge=0)
    application_versions: int = Field(ge=0)
    document_versions: int = Field(ge=0)
    comment_versions: int = Field(ge=0)
    pending_retries: int = Field(ge=0)
    retry_entries: int = Field(ge=0)
    failed_sections: int = Field(ge=0)
    unmapped_records: int = Field(ge=0)


class QualificationCost(_ReceiptModel):
    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class ZeroNetworkCost(_ReceiptModel):
    request_count: Literal[0]
    transferred_bytes: Literal[0]
    attachment_body_requests: Literal[0]


class QualificationCosts(_ReceiptModel):
    initial: QualificationCost
    rerun: ZeroNetworkCost


class QualificationCheck(_ReceiptModel):
    name: str
    ok: bool


class RetryPolicy(_ReceiptModel):
    max_attempts: Literal[1] = 1


class WeeklyCycle(_ReceiptModel):
    cycle: Literal[1, 2]
    eligible_on: date
    status: Literal["pending"] = "pending"


class QualificationProofLink(_ReceiptModel):
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QualificationEvidenceCommitment(_ReceiptModel):
    canonicalization: Literal["sha256-canonical-json-v1"]
    applications: int = Field(ge=1)
    capture_associations: int = Field(ge=1)
    content_digests: int = Field(ge=1)
    retained_evidence_rows: int = Field(ge=1)
    minimum_captures_per_application: int = Field(ge=1)
    maximum_captures_per_application: int = Field(ge=1)
    missing_paths: Literal[0]
    invalid_paths: Literal[0]
    application_capture_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_digest_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retained_evidence_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _EvidenceCaptureCommitmentInput(_ReceiptModel):
    digest: str
    source_url: str
    media_type: str


class _ApplicationCaptureCommitmentInput(_ReceiptModel):
    identity: QualificationIdentity
    captures: tuple[_EvidenceCaptureCommitmentInput, ...] = Field(min_length=1)


class _PeakDistrictQualificationReceiptV1(_ReceiptModel):
    schema_version: Literal[1] = 1
    authority_id: Literal["peak-district"] = "peak-district"
    source_contract: Literal["assure-live-v1"] = "assure-live-v1"
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[QualificationQuery, ...]
    retry_policy: RetryPolicy = RetryPolicy()
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    evidence_commitment: QualificationEvidenceCommitment
    weekly_cycles: tuple[WeeklyCycle, WeeklyCycle]


class PeakDistrictSanitizedQualificationReceiptV1(_PeakDistrictQualificationReceiptV1):
    pass


class PeakDistrictQualificationReceiptV1(_PeakDistrictQualificationReceiptV1):
    identities: tuple[QualificationIdentity, ...] = Field(min_length=1)


class PeakDistrictWeeklyQualificationReceiptV1(_ReceiptModel):
    schema_version: Literal[1] = 1
    authority_id: Literal["peak-district"] = "peak-district"
    source_contract: Literal["assure-live-v1"] = "assure-live-v1"
    cycle: WeeklyCycleNumber
    created_at: datetime
    scope: QualificationScope
    query_inventory: tuple[QualificationQuery, ...]
    prior_proof: QualificationProofLink
    identities: tuple[QualificationIdentity, ...] = Field(min_length=1)
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    authority_run_count: int = Field(ge=2)
    run_history_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checks: tuple[QualificationCheck, ...]
    evidence_commitment: QualificationEvidenceCommitment


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScope


class _BootstrapProof(FrozenModel):
    receipt: PeakDistrictQualificationReceiptV1
    path: Path


class _WeeklyQualificationRequest(FrozenModel):
    cycle: WeeklyCycleNumber
    prior_proof: QualificationProofLink
    created_at: datetime


class _WeeklyQualificationBuild(FrozenModel):
    request: _WeeklyQualificationRequest
    initial: QualificationCost
    run_statuses: tuple[RunStatus, ...]


class QualificationConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class QualificationFailedError(RuntimeError):
    def __init__(self, failed_checks: tuple[str, ...]) -> None:
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


def _checkpoint(store: SqliteStore) -> PeakDistrictCheckpointV1 | None:
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
        include_open=True,
    )


def _expected_inventory(
    scope: QualificationScope,
) -> tuple[QualificationQuery, ...]:
    return tuple(
        QualificationQuery.model_validate(query.model_dump())
        for query in peak_district_query_inventory(
            DiscoveryWindow(
                start=scope.start,
                end=scope.end,
                include_open=True,
            )
        )
    )


def _exact_query_inventory(
    checkpoint: PeakDistrictCheckpointV1 | None,
    scope: QualificationScope,
) -> bool:
    if checkpoint is None:
        return False
    expected = tuple(
        query.key
        for query in peak_district_query_inventory(
            DiscoveryWindow(start=scope.start, end=scope.end, include_open=True)
        )
    )
    return checkpoint.completed_queries == expected


def _identity(reference: SourceReference) -> tuple[str, str, str | None]:
    return str(reference.source_id), reference.reference, reference.locator


def _terminal_checkpoint(
    store: SqliteStore,
    scope: QualificationScope,
) -> PeakDistrictCheckpointV1 | None:
    checkpoint = _checkpoint(store)
    if checkpoint is None:
        return None
    state = store.discovery_state(_AUTHORITY_ID)
    expected = {
        (str(LEGACY_SOURCE), reference) for reference in checkpoint.seen_references
    }
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
        checkpoint.row_offset == "live"
        and checkpoint.live_scope == _expected_scope(scope)
        and checkpoint.live_complete
        and _exact_query_inventory(checkpoint, scope)
        and checkpoint.active_query is None
        and checkpoint.next_page_index == 0
        and checkpoint.query_row_count == 0
        and bool(expected)
        and len(expected) == len(checkpoint.seen_references)
        and expected.issubset(
            {(source, reference) for source, reference, _locator in durable}
        )
        and expected.issubset(
            {(source, reference) for source, reference, _locator in retained}
        )
        and len(durable) == len(set(durable))
        and len(retained) == len(set(retained))
        and all(locator for _source, _reference, locator in durable)
        and {item for item in durable if (item[0], item[1]) in expected}
        == {item for item in retained if (item[0], item[1]) in expected}
    ):
        return None
    return checkpoint


def _registration_matches(
    registration: RetainedDiscoveryEvidenceRegistration,
    request: PortalRequest,
    *,
    response_url: str,
) -> bool:
    return (
        registration.response_url == response_url
        and registration.request_url == str(request.url)
        and registration.request_method == request.method.value
        and registration.request_form
        == tuple((field.name, field.value) for field in request.form)
    )


def _run_discovery_evidence_complete(
    query: PeakDistrictQueryV1,
    pages: tuple[PeakDistrictDiscoveryPageV1, ...],
    registrations: tuple[RetainedDiscoveryEvidenceRegistration, ...],
    captures: dict[str, EvidenceCapture],
    run_id: str,
) -> bool:
    scoped = tuple(item for item in registrations if item.run_id == run_id)
    first_page = pages[0]
    first_scope = tuple(
        item
        for item in scoped
        if item.query_key == first_page.query_key and item.page == 1
    )
    search_form = next(
        (
            item
            for item in first_scope
            if item.request_url == _SEARCH_URL
            and item.request_method == "GET"
            and item.request_form == ()
            and item.response_url == _SEARCH_URL
        ),
        None,
    )
    advanced_form = next(
        (
            item
            for item in first_scope
            if item.request_url == _ADVANCED_FORM_URL
            and item.request_method == "GET"
            and item.request_form == ()
            and item.response_url == _ADVANCED_FORM_URL
        ),
        None,
    )
    search_capture = (
        None if search_form is None else captures.get(str(search_form.digest))
    )
    advanced_capture = (
        None if advanced_form is None else captures.get(str(advanced_form.digest))
    )
    if search_capture is None or advanced_capture is None:
        return False
    try:
        form = _parse_search_form(search_capture.body, advanced_capture.body)
        query_request = _query_request(form, query)
    except ValueError:
        return False
    prior_page = None
    valid = True
    for proof in pages:
        capture = captures.get(str(proof.digest))
        if capture is None:
            valid = False
            break
        try:
            expected_request = (
                query_request
                if proof.page_index == 0
                else _pagination_request(
                    query_request.form,
                    None if prior_page is None else prior_page.form,
                    proof.page_index,
                )
            )
        except ValueError:
            valid = False
            break
        candidates = tuple(
            item
            for item in scoped
            if item.query_key == proof.query_key
            and item.page == proof.page_index + 1
            and str(item.digest) == str(proof.digest)
        )
        if len(candidates) != 1 or not _registration_matches(
            candidates[0],
            expected_request,
            response_url=str(expected_request.url),
        ):
            valid = False
            break
        try:
            parsed = _parse_search_page(
                capture.body,
                expected_page=proof.page_index,
            )
        except ValueError:
            valid = False
            break
        if (
            tuple(item.reference for item in parsed.references) != proof.references
            or parsed.reported != proof.reported
            or parsed.page_size != proof.page_size
        ):
            valid = False
            break
        prior_page = parsed
    return valid


def _discovery_evidence_complete(
    store: SqliteStore,
    checkpoint: PeakDistrictCheckpointV1 | None,
    scope: QualificationScope,
) -> bool:
    if checkpoint is None or not checkpoint.discovery_pages:
        return False
    queries = peak_district_query_inventory(
        DiscoveryWindow(start=scope.start, end=scope.end, include_open=True)
    )
    discovery_scope = _expected_scope(scope)
    pages_by_query = {
        query.key: tuple(
            page
            for page in checkpoint.discovery_pages
            if page.query_key == peak_district_evidence_key(discovery_scope, query)
        )
        for query in queries
    }
    all_references: list[str] = []
    for query in queries:
        pages = pages_by_query[query.key]
        if (
            not pages
            or tuple(page.page_index for page in pages) != tuple(range(len(pages)))
            or len({page.reported for page in pages}) != 1
            or len({page.page_size for page in pages}) != 1
            or sum(len(page.references) for page in pages) != pages[0].reported
        ):
            return False
        all_references.extend(
            reference for page in pages for reference in page.references
        )
    if set(all_references) != set(checkpoint.seen_references):
        return False
    try:
        audit: EvidenceRegistrationAudit = store.evidence_registration_audit(
            _AUTHORITY_ID
        )
        captures = {
            str(capture.digest): capture for capture in store.retained_evidence()
        }
    except (EvidenceIntegrityError, KeyError, OSError, ValueError):
        return False
    for query in queries:
        pages = pages_by_query[query.key]
        first = pages[0]
        run_ids = {
            item.run_id
            for item in audit.discovery_registrations
            if item.query_key == first.query_key
            and item.page == 1
            and str(item.digest) == str(first.digest)
        }
        if not any(
            _run_discovery_evidence_complete(
                query,
                pages,
                audit.discovery_registrations,
                captures,
                run_id,
            )
            for run_id in run_ids
        ):
            return False
    return True


def _scope_compatible(store: SqliteStore, scope: QualificationScope) -> bool:
    state = store.discovery_state(_AUTHORITY_ID)
    if state.checkpoint is None:
        return True
    checkpoint = _checkpoint(store)
    if checkpoint is None or checkpoint.live_scope is None:
        return True
    return checkpoint.live_scope == _expected_scope(scope)


def _valid_record_evidence(record: RetainedNativeRecord) -> bool:
    locator = record.reference.locator
    captures = record.evidence
    if (
        locator is None
        or len(captures) < _MINIMUM_APPLICATION_CAPTURES
        or str(captures[0].url) != locator
    ):
        return False
    locator_parts = urlsplit(locator)
    if (
        locator_parts.scheme != "https"
        or locator_parts.netloc != "planning.peakdistrict.gov.uk"
        or locator_parts.path != _DETAIL_PATH
        or parse_qs(locator_parts.query).get("applicationNumber")
        != [record.reference.reference]
    ):
        return False
    for page_index, capture in enumerate(captures[1:]):
        parts = urlsplit(str(capture.url))
        query = parse_qs(parts.query)
        if (
            parts.scheme != "https"
            or parts.netloc != "planning.peakdistrict.gov.uk"
            or parts.path != _DOCUMENTS_PATH
            or query.get("applicationNumber") != [record.reference.reference]
            or query.get("currentPageIndex") != [str(page_index)]
            or query.get("pageSize") != ["10"]
            or query.get("IsDatePublishSortedDescending") != ["false"]
            or _ATTACHMENT_PATH.casefold() in parts.path.casefold()
        ):
            return False
    return True


def _qualified_records(
    store: SqliteStore,
    checkpoint: PeakDistrictCheckpointV1 | None,
) -> tuple[RetainedNativeRecord, ...] | None:
    if checkpoint is None:
        return None
    try:
        records = tuple(
            record
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
            and record.reference.reference in checkpoint.seen_references
        )
    except (EvidenceIntegrityError, KeyError):
        return None
    if (
        {record.reference.reference for record in records}
        != set(checkpoint.seen_references)
        or len(records) != len(checkpoint.seen_references)
        or not all(_valid_record_evidence(record) for record in records)
    ):
        return None
    return records


def _sha256_commitment(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{sha256(payload).hexdigest()}"


def _file_sha256(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


def _cycle_proof_path(data_dir: Path, cycle: WeeklyCycleNumber) -> Path:
    return data_dir / f"peak-district-weekly-cycle-{cycle}-proof-v1.json"


def _cycle_receipt_path(data_dir: Path, cycle: WeeklyCycleNumber) -> Path:
    return data_dir / f"peak-district-weekly-cycle-{cycle}-receipt-v1.json"


def _run_history_sha256(store: SqliteStore) -> str:
    return _sha256_commitment(
        [run.model_dump(mode="json") for run in store.run_costs(_AUTHORITY_ID)]
    )


def _latest_successful_pair_cost(store: SqliteStore) -> QualificationCost | None:
    runs = store.run_costs(_AUTHORITY_ID)
    if (
        len(runs) < _MINIMUM_QUALIFICATION_RUNS
        or runs[-2].status != RunStatus.SUCCEEDED
        or runs[-1].status != RunStatus.SUCCEEDED
        or runs[-1].request_count != 0
        or runs[-1].transferred_bytes != 0
        or runs[-2].request_count == 0
        or runs[-2].transferred_bytes == 0
    ):
        return None
    return QualificationCost(
        request_count=runs[-2].request_count,
        transferred_bytes=runs[-2].transferred_bytes,
        attachment_body_requests=0,
    )


def _evidence_commitment(
    store: SqliteStore,
    checkpoint: PeakDistrictCheckpointV1 | None,
) -> QualificationEvidenceCommitment | None:
    records = _qualified_records(store, checkpoint)
    if records is None:
        return None
    applications = tuple(
        sorted(
            (
                _ApplicationCaptureCommitmentInput(
                    identity=QualificationIdentity(
                        reference=record.reference.reference,
                        locator=record.reference.locator or "",
                    ),
                    captures=tuple(
                        _EvidenceCaptureCommitmentInput(
                            digest=str(capture.digest),
                            source_url=str(capture.url),
                            media_type=capture.media_type,
                        )
                        for capture in record.evidence
                    ),
                )
                for record in records
            ),
            key=lambda application: (
                application.identity.source_id,
                application.identity.reference,
                application.identity.locator,
            ),
        )
    )
    content_digests = tuple(
        sorted(
            {
                capture.digest
                for application in applications
                for capture in application.captures
            }
        )
    )
    missing_paths = store.missing_evidence_paths()
    invalid_paths = store.invalid_evidence_paths()
    if missing_paths or invalid_paths:
        return None
    try:
        retained_evidence = tuple(
            sorted(
                (
                    {
                        "digest": str(capture.digest),
                        "source_url": str(capture.url),
                        "media_type": capture.media_type,
                    }
                    for capture in store.retained_evidence()
                ),
                key=lambda capture: capture["digest"],
            )
        )
    except (EvidenceIntegrityError, KeyError, OSError, ValueError):
        return None
    if not retained_evidence:
        return None
    capture_counts = tuple(len(application.captures) for application in applications)
    return QualificationEvidenceCommitment(
        canonicalization=_COMMITMENT_CANONICALIZATION,
        applications=len(applications),
        capture_associations=sum(capture_counts),
        content_digests=len(content_digests),
        retained_evidence_rows=len(retained_evidence),
        minimum_captures_per_application=min(capture_counts),
        maximum_captures_per_application=max(capture_counts),
        missing_paths=0,
        invalid_paths=0,
        application_capture_sha256=_sha256_commitment(
            [application.model_dump(mode="json") for application in applications]
        ),
        content_digest_set_sha256=_sha256_commitment(content_digests),
        retained_evidence_sha256=_sha256_commitment(retained_evidence),
    )


def _counts(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
) -> QualificationCounts:
    return QualificationCounts.model_validate(
        {
            **snapshot.model_dump(exclude={"authority_id"}),
            "retry_entries": sum(
                item.authority_id == _AUTHORITY_ID for item in store.retry_items()
            ),
        }
    )


def _durable_bootstrap_cost(store: SqliteStore) -> QualificationCost | None:
    runs = store.run_costs(_AUTHORITY_ID)
    if (
        len(runs) < _MINIMUM_QUALIFICATION_RUNS
        or runs[-1].status != RunStatus.SUCCEEDED
        or runs[-1].request_count != 0
        or runs[-1].transferred_bytes != 0
        or runs[-2].status != RunStatus.SUCCEEDED
        or any(run.status == RunStatus.RUNNING for run in runs[:-1])
    ):
        return None
    request_count = sum(run.request_count for run in runs[:-1])
    transferred_bytes = sum(run.transferred_bytes for run in runs[:-1])
    if request_count == 0 or transferred_bytes == 0:
        return None
    return QualificationCost(
        request_count=request_count,
        transferred_bytes=transferred_bytes,
        attachment_body_requests=0,
    )


def _base_checks(
    store: SqliteStore,
    snapshot: QualificationSnapshot,
    scope: QualificationScope,
    initial: QualificationCost,
) -> tuple[QualificationCheck, ...]:
    checkpoint = _terminal_checkpoint(store, scope)
    discovery_evidence_ok = checkpoint is not None and (
        not checkpoint.discovery_pages
        or _discovery_evidence_complete(store, checkpoint, scope)
    )
    authority = next(
        (
            state
            for state in store.authority_states()
            if state.manifest.id == _AUTHORITY_ID
        ),
        None,
    )
    return (
        QualificationCheck(name="terminal-checkpoint", ok=checkpoint is not None),
        QualificationCheck(
            name="exact-query-inventory",
            ok=_exact_query_inventory(checkpoint, scope),
        ),
        QualificationCheck(
            name="reference-application-agreement",
            ok=_qualified_records(store, checkpoint) is not None,
        ),
        QualificationCheck(
            name="pending-retries",
            ok=snapshot.pending_retries == 0,
        ),
        QualificationCheck(
            name="retry-inventory",
            ok=not any(
                item.authority_id == _AUTHORITY_ID for item in store.retry_items()
            ),
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
            ok=not store.invalid_evidence_paths() and discovery_evidence_ok,
        ),
        QualificationCheck(
            name="application-evidence",
            ok=_qualified_records(store, checkpoint) is not None,
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


def _weekly_cycles(created_at: datetime) -> tuple[WeeklyCycle, WeeklyCycle]:
    return (
        WeeklyCycle(cycle=1, eligible_on=created_at.date() + timedelta(days=7)),
        WeeklyCycle(cycle=2, eligible_on=created_at.date() + timedelta(days=14)),
    )


def _identities(
    records: tuple[RetainedNativeRecord, ...],
) -> tuple[QualificationIdentity, ...]:
    by_reference = {record.reference.reference: record for record in records}
    return tuple(
        QualificationIdentity(
            reference=reference,
            locator=by_reference[reference].reference.locator or "",
        )
        for reference in sorted(by_reference)
    )


def _receipt_from_store(
    store: SqliteStore,
    config: _Config,
    created_at: datetime,
    run_statuses: tuple[RunStatus, ...],
) -> PeakDistrictQualificationReceiptV1:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    bootstrap_cost = _durable_bootstrap_cost(store)
    if bootstrap_cost is None:
        raise QualificationFailedError(("bootstrap-provenance",))
    terminal = _terminal_checkpoint(store, config.scope)
    records = _qualified_records(store, terminal)
    commitment = _evidence_commitment(store, terminal)
    if terminal is None or records is None or commitment is None:
        raise QualificationFailedError(("application-evidence",))
    final_checks = (
        *_base_checks(store, snapshot, config.scope, bootstrap_cost),
        QualificationCheck(name="idempotent-rerun", ok=True),
        QualificationCheck(name="terminal-rerun-requests", ok=True),
        QualificationCheck(
            name="run-statuses",
            ok=run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    return PeakDistrictQualificationReceiptV1(
        created_at=created_at,
        scope=config.scope,
        query_inventory=_expected_inventory(config.scope),
        identities=_identities(records),
        counts=_counts(store, snapshot),
        costs=QualificationCosts(
            initial=bootstrap_cost,
            rerun=ZeroNetworkCost(
                request_count=0,
                transferred_bytes=0,
                attachment_body_requests=0,
            ),
        ),
        run_statuses=run_statuses,
        checks=final_checks,
        evidence_commitment=commitment,
        weekly_cycles=_weekly_cycles(created_at),
    )


async def _qualify(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> PeakDistrictQualificationReceiptV1:
    registry = AuthorityRegistry((PEAK_DISTRICT_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    prior_run_count = len(store.run_costs(_AUTHORITY_ID))
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    _require(_base_checks(store, first_snapshot, config.scope, initial))

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    runs = store.run_costs(_AUTHORITY_ID)[prior_run_count:]
    run_statuses = tuple(run.status for run in runs)
    final_checks = (
        QualificationCheck(
            name="idempotent-rerun", ok=first_snapshot == final_snapshot
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
    return _receipt_from_store(store, config, now(), run_statuses)


def _weekly_receipt_from_store(
    store: SqliteStore,
    config: _Config,
    build: _WeeklyQualificationBuild,
) -> PeakDistrictWeeklyQualificationReceiptV1:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    terminal = _terminal_checkpoint(store, config.scope)
    records = _qualified_records(store, terminal)
    commitment = _evidence_commitment(store, terminal)
    durable_cost = _latest_successful_pair_cost(store)
    if (
        terminal is None
        or records is None
        or commitment is None
        or durable_cost != build.initial
    ):
        raise QualificationFailedError(("application-evidence",))
    final_checks = (
        *_base_checks(store, snapshot, config.scope, build.initial),
        QualificationCheck(name="idempotent-rerun", ok=True),
        QualificationCheck(name="terminal-rerun-requests", ok=True),
        QualificationCheck(
            name="run-statuses",
            ok=build.run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        ),
    )
    _require(final_checks)
    runs = store.run_costs(_AUTHORITY_ID)
    return PeakDistrictWeeklyQualificationReceiptV1(
        cycle=build.request.cycle,
        created_at=build.request.created_at,
        scope=config.scope,
        query_inventory=_expected_inventory(config.scope),
        prior_proof=build.request.prior_proof,
        identities=_identities(records),
        counts=_counts(store, snapshot),
        costs=QualificationCosts(
            initial=build.initial,
            rerun=ZeroNetworkCost(
                request_count=0,
                transferred_bytes=0,
                attachment_body_requests=0,
            ),
        ),
        run_statuses=build.run_statuses,
        authority_run_count=len(runs),
        run_history_sha256=_run_history_sha256(store),
        checks=final_checks,
        evidence_commitment=commitment,
    )


async def _qualify_weekly(
    store: SqliteStore,
    config: _Config,
    request: _WeeklyQualificationRequest,
    session_factory: SessionFactory,
) -> PeakDistrictWeeklyQualificationReceiptV1:
    registry = AuthorityRegistry((PEAK_DISTRICT_PACKAGE,), PILOT_LIVE_STATUS)
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=True,
    )
    prior_run_count = len(store.run_costs(_AUTHORITY_ID))
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    _require(_base_checks(store, first_snapshot, config.scope, initial))
    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    runs = store.run_costs(_AUTHORITY_ID)[prior_run_count:]
    run_statuses = tuple(run.status for run in runs)
    final_checks = (
        QualificationCheck(
            name="idempotent-rerun", ok=first_snapshot == final_snapshot
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
    return _weekly_receipt_from_store(
        store,
        config,
        _WeeklyQualificationBuild(
            request=request,
            initial=initial,
            run_statuses=run_statuses,
        ),
    )


def _receipt_matches_store(
    store: SqliteStore,
    config: _Config,
    receipt: PeakDistrictQualificationReceiptV1,
    records: tuple[RetainedNativeRecord, ...],
    commitment: QualificationEvidenceCommitment,
) -> bool:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    runs = store.run_costs(_AUTHORITY_ID)
    current_checks = _base_checks(store, snapshot, config.scope, receipt.costs.initial)
    return (
        receipt.scope == config.scope
        and receipt.query_inventory == _expected_inventory(config.scope)
        and receipt.identities == _identities(records)
        and receipt.counts == _counts(store, snapshot)
        and receipt.costs.initial == _durable_bootstrap_cost(store)
        and receipt.costs.rerun
        == ZeroNetworkCost(
            request_count=0,
            transferred_bytes=0,
            attachment_body_requests=0,
        )
        and receipt.run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED)
        and tuple(run.status for run in runs[-2:]) == receipt.run_statuses
        and tuple(check.name for check in receipt.checks) == _CHECK_NAMES
        and all(check.ok for check in receipt.checks)
        and all(check.ok for check in current_checks)
        and receipt.evidence_commitment == commitment
        and receipt.weekly_cycles == _weekly_cycles(receipt.created_at)
    )


def _weekly_receipt_matches_store(
    store: SqliteStore,
    config: _Config,
    receipt: PeakDistrictWeeklyQualificationReceiptV1,
    prior_path: Path,
) -> bool:
    snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    runs = store.run_costs(_AUTHORITY_ID)
    terminal = _terminal_checkpoint(store, config.scope)
    records = _qualified_records(store, terminal)
    commitment = _evidence_commitment(store, terminal)
    if records is None or commitment is None:
        return False
    current_checks = _base_checks(
        store,
        snapshot,
        config.scope,
        receipt.costs.initial,
    )
    try:
        prior_sha256 = _file_sha256(prior_path)
    except OSError:
        return False
    return (
        receipt.scope == config.scope
        and receipt.query_inventory == _expected_inventory(config.scope)
        and receipt.prior_proof
        == QualificationProofLink(
            filename=prior_path.name,
            sha256=prior_sha256,
        )
        and receipt.identities == _identities(records)
        and receipt.counts == _counts(store, snapshot)
        and receipt.costs.initial == _latest_successful_pair_cost(store)
        and receipt.costs.rerun
        == ZeroNetworkCost(
            request_count=0,
            transferred_bytes=0,
            attachment_body_requests=0,
        )
        and receipt.run_statuses == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED)
        and tuple(run.status for run in runs[-2:]) == receipt.run_statuses
        and receipt.authority_run_count == len(runs)
        and receipt.run_history_sha256 == _run_history_sha256(store)
        and tuple(check.name for check in receipt.checks) == _CHECK_NAMES
        and all(check.ok for check in receipt.checks)
        and all(check.ok for check in current_checks)
        and receipt.evidence_commitment == commitment
    )


def _read_weekly_receipt(
    path: Path,
    store: SqliteStore,
    config: _Config,
    prior_path: Path,
) -> PeakDistrictWeeklyQualificationReceiptV1:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailedError(("cycle-provenance",)) from error
    if not isinstance(payload, dict):
        raise QualificationFailedError(("cycle-provenance",))
    try:
        receipt = PeakDistrictWeeklyQualificationReceiptV1.model_validate(payload)
    except ValueError as error:
        raise QualificationFailedError(("cycle-provenance",)) from error
    if not _weekly_receipt_matches_store(
        store,
        config,
        receipt,
        prior_path,
    ):
        raise QualificationFailedError(("cycle-provenance",))
    return receipt


def _read_receipt(
    path: Path,
    store: SqliteStore,
    config: _Config,
) -> PeakDistrictQualificationReceiptV1:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailedError(("bootstrap-provenance",)) from error
    terminal = _terminal_checkpoint(store, config.scope)
    records = _qualified_records(store, terminal)
    commitment = _evidence_commitment(store, terminal)
    if (
        not isinstance(payload, dict)
        or terminal is None
        or records is None
        or commitment is None
    ):
        raise QualificationFailedError(("bootstrap-provenance",))
    try:
        receipt = PeakDistrictQualificationReceiptV1.model_validate(payload)
    except ValueError as error:
        raise QualificationFailedError(("bootstrap-provenance",)) from error
    if not _receipt_matches_store(
        store,
        config,
        receipt,
        records,
        commitment,
    ):
        raise QualificationFailedError(("bootstrap-provenance",))
    return receipt


def _temporary_receipt_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp")


def _read_typed_document[ReceiptT: _ReceiptModel](
    path: Path,
    model: type[ReceiptT],
    failure: str,
) -> ReceiptT:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise QualificationFailedError((failure,)) from error


def _proof_link(path: Path) -> QualificationProofLink:
    try:
        return QualificationProofLink(
            filename=path.name,
            sha256=_file_sha256(path),
        )
    except OSError as error:
        raise QualificationFailedError(("cycle-provenance",)) from error


def _prior_link_matches(
    receipt: PeakDistrictWeeklyQualificationReceiptV1,
    path: Path,
) -> bool:
    try:
        return receipt.prior_proof == _proof_link(path)
    except QualificationFailedError:
        return False


def _read_or_recover_cycle_proof(
    path: Path,
    store: SqliteStore,
    data_dir: Path,
    prior_path: Path,
    cycle: WeeklyCycleNumber,
) -> PeakDistrictWeeklyQualificationReceiptV1 | None:
    temporary = _temporary_receipt_path(path)
    candidate = path if path.is_file() else temporary
    if not candidate.is_file():
        return None
    receipt = _read_typed_document(
        candidate,
        PeakDistrictWeeklyQualificationReceiptV1,
        "cycle-provenance",
    )
    if receipt.cycle != cycle or not _prior_link_matches(receipt, prior_path):
        raise QualificationFailedError(("cycle-provenance",))
    if candidate == path:
        return receipt
    receipt = _read_weekly_receipt(
        candidate,
        store,
        _Config(data_dir=data_dir, scope=receipt.scope),
        prior_path,
    )
    _write_receipt(path, receipt)
    return receipt


async def _qualify_or_recover_weekly(
    store: SqliteStore,
    config: _Config,
    bootstrap_proof: _BootstrapProof,
    session_factory: SessionFactory,
    now: Clock,
) -> PeakDistrictWeeklyQualificationReceiptV1:
    bootstrap = bootstrap_proof.receipt
    bootstrap_path = bootstrap_proof.path
    cycle_one_path = _cycle_proof_path(config.data_dir, 1)
    cycle_two_path = _cycle_proof_path(config.data_dir, 2)
    cycle_one = _read_or_recover_cycle_proof(
        cycle_one_path,
        store,
        config.data_dir,
        bootstrap_path,
        1,
    )
    cycle_two_exists = (
        cycle_two_path.is_file() or _temporary_receipt_path(cycle_two_path).is_file()
    )
    if cycle_one is None and cycle_two_exists:
        raise QualificationFailedError(("cycle-provenance",))
    cycle_two = (
        _read_or_recover_cycle_proof(
            cycle_two_path,
            store,
            config.data_dir,
            cycle_one_path,
            _SECOND_WEEKLY_CYCLE,
        )
        if cycle_one is not None
        else None
    )
    if cycle_two is not None:
        if config.scope != cycle_two.scope:
            raise QualificationConfigError(_WEEKLY_CYCLES_COMPLETE)
        return _read_weekly_receipt(
            cycle_two_path,
            store,
            config,
            cycle_one_path,
        )
    if cycle_one is not None:
        if config.scope == cycle_one.scope:
            return _read_weekly_receipt(
                cycle_one_path,
                store,
                config,
                bootstrap_path,
            )
        _read_weekly_receipt(
            cycle_one_path,
            store,
            _Config(data_dir=config.data_dir, scope=cycle_one.scope),
            bootstrap_path,
        )
        cycle: WeeklyCycleNumber = 2
        prior_path = cycle_one_path
        prior_scope = cycle_one.scope
    else:
        _read_receipt(
            bootstrap_path,
            store,
            _Config(data_dir=config.data_dir, scope=bootstrap.scope),
        )
        cycle = 1
        prior_path = bootstrap_path
        prior_scope = bootstrap.scope
    created_at = now()
    eligible_on = bootstrap.weekly_cycles[cycle - 1].eligible_on
    if (
        created_at.date() < eligible_on
        or config.scope.end < eligible_on
        or config.scope.end <= prior_scope.end
    ):
        raise QualificationConfigError(_CYCLE_NOT_ELIGIBLE)
    receipt = await _qualify_weekly(
        store,
        config,
        _WeeklyQualificationRequest(
            cycle=cycle,
            prior_proof=_proof_link(prior_path),
            created_at=created_at,
        ),
        session_factory,
    )
    _write_receipt(_cycle_proof_path(config.data_dir, cycle), receipt)
    return receipt


async def _qualify_or_recover(
    store: SqliteStore,
    config: _Config,
    session_factory: SessionFactory,
    now: Clock,
) -> PeakDistrictQualificationReceiptV1 | PeakDistrictWeeklyQualificationReceiptV1:
    proof_path = config.data_dir / _PROOF_NAME
    public_path = config.data_dir / _RECEIPT_NAME
    if proof_path.is_file():
        bootstrap = _read_typed_document(
            proof_path,
            PeakDistrictQualificationReceiptV1,
            "bootstrap-provenance",
        )
        if bootstrap.scope != config.scope:
            return await _qualify_or_recover_weekly(
                store,
                config,
                _BootstrapProof(receipt=bootstrap, path=proof_path),
                session_factory,
                now,
            )
    candidates = (
        proof_path,
        _temporary_receipt_path(proof_path),
        public_path,
        _temporary_receipt_path(public_path),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        receipt = _read_receipt(candidate, store, config)
        if candidate != proof_path:
            _write_receipt(proof_path, receipt)
        return receipt
    if _terminal_checkpoint(store, config.scope) is not None or any(
        run.status == RunStatus.RUNNING for run in store.run_costs(_AUTHORITY_ID)
    ):
        raise QualificationFailedError(("bootstrap-provenance",))
    receipt = await _qualify(store, config, session_factory, now)
    _write_receipt(proof_path, receipt)
    return receipt


def _write_receipt(
    path: Path,
    receipt: PeakDistrictQualificationReceiptV1
    | PeakDistrictWeeklyQualificationReceiptV1,
) -> None:
    temporary = _temporary_receipt_path(path)
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


def _sanitized_receipt(
    receipt: PeakDistrictQualificationReceiptV1,
) -> PeakDistrictSanitizedQualificationReceiptV1:
    return PeakDistrictSanitizedQualificationReceiptV1.model_validate(
        receipt.model_dump(exclude={"identities"})
    )


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
                if (
                    not _scope_compatible(store, config.scope)
                    and not (config.data_dir / _PROOF_NAME).is_file()
                ):
                    raise QualificationConfigError(_SCOPE_MISMATCH)
                receipt = asyncio.run(
                    _qualify_or_recover(store, config, session_factory, now)
                )
                receipt_path = (
                    config.data_dir / _RECEIPT_NAME
                    if isinstance(receipt, PeakDistrictQualificationReceiptV1)
                    else _cycle_receipt_path(config.data_dir, receipt.cycle)
                )
                _write_receipt(receipt_path, receipt)
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
    except (LookupError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        return _error("runtime-failure", 1, exception=type(error).__name__)
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
