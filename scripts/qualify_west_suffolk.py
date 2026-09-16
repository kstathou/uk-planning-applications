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
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from yimby.authorities.west_suffolk import WEST_SUFFOLK_PACKAGE
from yimby.authorities.west_suffolk.adapter import (
    WestSuffolkCheckpointV1,
    WestSuffolkDiscoveryProofV1,
    WestSuffolkDiscoveryScope,
    validate_west_suffolk_discovery_proof,
)
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    FrozenModel,
    QualificationSnapshot,
    RunStatus,
)
from yimby.evidence import EvidenceIntegrityError, EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import (
    EvidenceRegistrationAudit,
    RetainedDiscoveryEvidenceRegistration,
    SqliteStore,
)
from yimby.transport import PortalSession

_AUTHORITY_ID = AuthorityId("west-suffolk")
_RECEIPT_NAME = "west-suffolk-qualification-v2.json"
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
_MINIMUM_QUALIFICATION_RUNS = 2


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


class QualificationEvidenceCommitment(FrozenModel):
    """Privacy-safe commitment to application and discovery captures."""

    canonicalization: Literal["sha256-canonical-json-v1"]
    applications: int = Field(ge=1)
    application_capture_associations: int = Field(ge=1)
    discovery_capture_associations: int = Field(ge=1)
    content_digests: int = Field(ge=1)
    retained_evidence_rows: int = Field(ge=1)
    missing_paths: int = Field(ge=0)
    invalid_paths: int = Field(ge=0)
    application_capture_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    discovery_capture_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_digest_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class WestSuffolkQualificationReceiptV2(FrozenModel):
    """Versioned result of a complete local live qualification."""

    schema_version: Literal[2] = 2
    authority_id: Literal["west-suffolk"] = "west-suffolk"
    created_at: datetime
    scope: QualificationScope
    counts: QualificationCounts
    costs: QualificationCosts
    run_statuses: tuple[RunStatus, ...]
    checks: tuple[QualificationCheck, ...]
    evidence_commitment: QualificationEvidenceCommitment


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


class _DiscoveryProofInvalidError(ValueError):
    """One retained discovery proof cannot establish its claimed subject."""


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
) -> WestSuffolkCheckpointV1 | None:
    state = store.discovery_state(_AUTHORITY_ID)
    stored = state.checkpoint
    if stored is None or stored.schema_version != 1:
        return None
    try:
        checkpoint = WestSuffolkCheckpointV1.model_validate_json(stored.payload_json)
    except ValueError:
        return None
    expected = WestSuffolkDiscoveryScope(
        start=scope.start,
        end=scope.end,
        include_open=scope.include_open,
    )
    seen = checkpoint.seen_references
    durable = state.references
    if not (
        checkpoint.result_page == "live"
        and checkpoint.live_scope == expected
        and checkpoint.live_complete
        and _completed_query_inventory(checkpoint, scope)
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and bool(durable)
        and len(seen) == len(set(seen))
        and set(seen).issubset(durable)
    ):
        return None
    return checkpoint


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


def _discovery_evidence_complete(
    store: SqliteStore,
    checkpoint: WestSuffolkCheckpointV1 | None,
    audit: EvidenceRegistrationAudit,
) -> bool:
    if checkpoint is None or not checkpoint.discovery_proofs:
        return False
    proofs = checkpoint.discovery_proofs
    proof_keys = tuple(proof.query_key for proof in proofs)
    if tuple(dict.fromkeys(proof_keys)) != checkpoint.completed_queries:
        return False
    try:
        captures = {
            capture.digest: capture
            for capture in store.discovery_evidence_captures(_AUTHORITY_ID)
        }
        seen = set().union(
            *(
                _query_proof_references(
                    query_key,
                    proofs,
                    captures,
                    audit.discovery_registrations,
                )
                for query_key in checkpoint.completed_queries
            )
        )
    except (EvidenceIntegrityError, KeyError, OSError, ValueError):
        return False
    return seen == set(checkpoint.seen_references)


def _query_proof_references(
    query_key: str,
    proofs: tuple[WestSuffolkDiscoveryProofV1, ...],
    captures: dict[EvidenceDigest, EvidenceCapture],
    registrations: tuple[RetainedDiscoveryEvidenceRegistration, ...],
) -> set[str]:
    query_proofs = tuple(proof for proof in proofs if proof.query_key == query_key)
    if tuple(proof.page for proof in query_proofs) != tuple(
        range(1, len(query_proofs) + 1)
    ):
        raise _DiscoveryProofInvalidError
    references: set[str] = set()
    row_count = 0
    reported: int | None = None
    for proof in query_proofs:
        if not _proof_is_registered(proof, registrations):
            raise _DiscoveryProofInvalidError
        capture = captures.get(proof.digest)
        if capture is None:
            raise _DiscoveryProofInvalidError
        parsed = validate_west_suffolk_discovery_proof(proof, capture)
        if reported is not None and parsed.reported != reported:
            raise _DiscoveryProofInvalidError
        reported = parsed.reported
        row_count += len(parsed.references)
        if row_count > reported or (
            proof.page < len(query_proofs) and row_count == reported
        ):
            raise _DiscoveryProofInvalidError
        references.update(item.reference for item in parsed.references)
    if reported is None or row_count != reported:
        raise _DiscoveryProofInvalidError
    return references


def _proof_is_registered(
    proof: WestSuffolkDiscoveryProofV1,
    registrations: tuple[RetainedDiscoveryEvidenceRegistration, ...],
) -> bool:
    matches = tuple(
        item
        for item in registrations
        if item.query_key == proof.query_key
        and item.page == proof.page
        and item.digest == proof.digest
    )
    return bool(matches) and all(
        item.response_url == str(proof.response_url)
        and item.request_url == str(proof.request_url)
        and item.request_method == proof.request_method
        and item.request_form == proof.request_form
        for item in matches
    )


def _sha256_commitment(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{sha256(payload).hexdigest()}"


def _evidence_commitment(
    store: SqliteStore,
    checkpoint: WestSuffolkCheckpointV1 | None,
    snapshot: QualificationSnapshot,
) -> QualificationEvidenceCommitment | None:
    audit = store.evidence_registration_audit(_AUTHORITY_ID)
    missing_paths = store.missing_evidence_paths()
    invalid_paths = store.invalid_evidence_paths()
    try:
        records = tuple(
            record
            for record in store.retained_native_records()
            if record.authority_id == _AUTHORITY_ID
        )
        retained_evidence = store.retained_evidence()
    except (EvidenceIntegrityError, KeyError, OSError, ValueError):
        return None
    if (
        checkpoint is None
        or len(records) != snapshot.applications
        or audit.application_count != snapshot.applications
        or audit.applications_with_evidence != snapshot.applications
        or audit.observation_count < snapshot.applications
        or audit.observations_with_evidence != audit.observation_count
        or audit.missing_digests
        or audit.unlinked_digests
        or not audit.current_rebuild_coherent
        or missing_paths
        or invalid_paths
        or not retained_evidence
        or not all(record.evidence for record in records)
        or not _discovery_evidence_complete(store, checkpoint, audit)
    ):
        return None
    applications = tuple(
        sorted(
            (
                {
                    "source_id": str(record.reference.source_id),
                    "reference": record.reference.reference,
                    "locator": record.reference.locator,
                    "captures": [
                        {
                            "digest": str(capture.digest),
                            "source_url": str(capture.url),
                            "media_type": capture.media_type,
                        }
                        for capture in record.evidence
                    ],
                }
                for record in records
            ),
            key=lambda item: (
                str(item["source_id"]),
                str(item["reference"]),
                str(item["locator"]),
            ),
        )
    )
    discovery = tuple(
        {
            "query_key": proof.query_key,
            "page": proof.page,
            "digest": str(proof.digest),
            "response_url": str(proof.response_url),
            "request_url": str(proof.request_url),
            "request_method": proof.request_method,
            "request_form": proof.request_form,
        }
        for proof in checkpoint.discovery_proofs
    )
    content_digests = tuple(
        sorted({str(capture.digest) for capture in retained_evidence})
    )
    return QualificationEvidenceCommitment(
        canonicalization="sha256-canonical-json-v1",
        applications=len(applications),
        application_capture_associations=sum(
            len(record.evidence) for record in records
        ),
        discovery_capture_associations=len(discovery),
        content_digests=len(content_digests),
        retained_evidence_rows=len(retained_evidence),
        missing_paths=0,
        invalid_paths=0,
        application_capture_sha256=_sha256_commitment(applications),
        discovery_capture_sha256=_sha256_commitment(discovery),
        content_digest_set_sha256=_sha256_commitment(content_digests),
    )


def _counts(snapshot: QualificationSnapshot) -> QualificationCounts:
    return QualificationCounts.model_validate(
        snapshot.model_dump(exclude={"authority_id"})
    )


def _durable_acquisition_cost(store: SqliteStore) -> QualificationCost | None:
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
    audit = store.evidence_registration_audit(_AUTHORITY_ID)
    discovery_evidence = _discovery_evidence_complete(store, checkpoint, audit)
    commitment = _evidence_commitment(store, checkpoint, snapshot)
    return (
        QualificationCheck(
            name="terminal-checkpoint",
            ok=checkpoint is not None,
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
            name="evidence-integrity",
            ok=not store.invalid_evidence_paths(),
        ),
        QualificationCheck(
            name="discovery-evidence",
            ok=discovery_evidence,
        ),
        QualificationCheck(
            name="application-evidence",
            ok=commitment is not None,
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
) -> WestSuffolkQualificationReceiptV2:
    registry = AuthorityRegistry((WEST_SUFFOLK_PACKAGE,))
    collector = Collector(registry, store)
    window = DiscoveryWindow(
        start=config.scope.start,
        end=config.scope.end,
        include_open=config.scope.include_open,
    )
    prior_run_count = len(store.run_costs(_AUTHORITY_ID))
    initial = await _collect_once(collector, window, session_factory)
    first_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    initial_checks = _base_checks(store, first_snapshot, config.scope, initial)
    _require(initial_checks)
    first_checkpoint = _terminal_checkpoint(store, config.scope)
    first_commitment = _evidence_commitment(
        store,
        first_checkpoint,
        first_snapshot,
    )
    if first_checkpoint is None or first_commitment is None:
        raise QualificationFailedError(("application-evidence",))

    rerun = await _collect_once(collector, window, session_factory)
    final_snapshot = store.qualification_snapshot(_AUTHORITY_ID)
    final_checkpoint = _terminal_checkpoint(store, config.scope)
    final_commitment = _evidence_commitment(
        store,
        final_checkpoint,
        final_snapshot,
    )
    runs = store.run_costs(_AUTHORITY_ID)[prior_run_count:]
    run_statuses = tuple(run.status for run in runs)
    final_checks = (
        *_base_checks(store, final_snapshot, config.scope, initial),
        QualificationCheck(
            name="idempotent-rerun",
            ok=(
                first_snapshot == final_snapshot
                and first_checkpoint == final_checkpoint
                and first_commitment == final_commitment
            ),
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
    acquisition_cost = _durable_acquisition_cost(store)
    if acquisition_cost is None or final_commitment is None:
        raise QualificationFailedError(("bootstrap-provenance",))
    return WestSuffolkQualificationReceiptV2(
        created_at=now(),
        scope=config.scope,
        counts=_counts(final_snapshot),
        costs=QualificationCosts(initial=acquisition_cost, rerun=rerun),
        run_statuses=run_statuses,
        checks=final_checks,
        evidence_commitment=final_commitment,
    )


def _read_prior_receipt(path: Path) -> WestSuffolkQualificationReceiptV2 | None:
    if not path.exists():
        return None
    try:
        return WestSuffolkQualificationReceiptV2.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise QualificationFailedError(("preserved-live-receipt",)) from error


def _receipt_to_persist(
    prior: WestSuffolkQualificationReceiptV2 | None,
    candidate: WestSuffolkQualificationReceiptV2,
) -> WestSuffolkQualificationReceiptV2:
    if prior is None or prior.scope != candidate.scope:
        return candidate
    if candidate.model_copy(update={"created_at": prior.created_at}) == prior:
        return prior
    raise QualificationFailedError(("preserved-live-receipt",))


def _write_receipt(
    path: Path,
    receipt: WestSuffolkQualificationReceiptV2,
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
                receipt_path = config.data_dir / _RECEIPT_NAME
                prior = _read_prior_receipt(receipt_path)
                candidate = asyncio.run(_qualify(store, config, session_factory, now))
                receipt = _receipt_to_persist(prior, candidate)
                if receipt is candidate:
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
