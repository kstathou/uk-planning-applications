# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Capture Cheshire East's live completeness blocker as durable evidence."""

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
from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator

import yimby.authorities.cheshire_east.adapter as cheshire
from yimby.domain import DiscoveryWindow, EvidenceCapture, FrozenModel
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.transport import PortalRequest, PortalSession, RequestMethod

_RECEIPT_NAME = "cheshire-east-qualification-blocker-v1.json"
_DETAIL_REFERENCE = "26/3335/PRIOR-1A"
_DETAIL_LOCATOR = "406569"
_HISTORICAL_WEEK = date(2024, 1, 1)
_CONFIRMATION_REQUIRED = "confirmation-required"
_INCLUDE_OPEN_REQUIRED = "include-open-required"
_INVALID_DATE = "invalid-date"
_INVALID_WINDOW = "invalid-window"
_EXACT_WINDOW_REQUIRED = "exact-30-day-window-required"
_DATA_DIR_NOT_DIRECTORY = "data-dir-not-directory"
_RESUME_REQUIRED = "resume-required"
_RECEIPT_REQUIRED = "receipt-required"

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationScopeV1(FrozenModel):
    """Exact inclusive bootstrap scope represented by the blocker artifact."""

    start: date
    end: date
    include_open: Literal[True] = True

    @model_validator(mode="after")
    def exact_window(self) -> Self:
        """Require one exact inclusive 30-day range."""
        if self.end - self.start != timedelta(days=29):
            raise ValueError(_EXACT_WINDOW_REQUIRED)
        return self


class RecordedFieldV1(FrozenModel):
    """One successful form field in source order."""

    name: str
    value: str


class RecordedQueryV1(FrozenModel):
    """One exact query included in the completeness decision."""

    key: str
    method: RequestMethod
    url: HttpUrl
    form: tuple[RecordedFieldV1, ...]


class RetainedEvidenceV1(FrozenModel):
    """One content-addressed response retained below the evidence root."""

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    source_url: HttpUrl
    media_type: str
    byte_count: int = Field(ge=0)


class RecentContractV1(FrozenModel):
    """Observed result of the exact recent valid-date request."""

    explicit_zero: bool
    visible_references: tuple[str, ...]


class WeeklyContractV1(FrozenModel):
    """Observed historical weekly boundary without inferred terminality."""

    week: date
    row_count: int = Field(ge=0)
    reported_total: int | None
    pagination_links: tuple[str, ...]
    terminal_marker: bool


class DocumentContractV1(FrozenModel):
    """One metadata-only document row from the detail response."""

    document_type: str
    description: str
    published_date: date
    url: HttpUrl


class DetailContractV1(FrozenModel):
    """Reference-verified direct detail evidence."""

    public_reference: str
    locator: str
    application_status: str
    valid_date: date
    document_count: int = Field(ge=0)
    documents: tuple[DocumentContractV1, ...]


class SourceContractV1(FrozenModel):
    """All official observations used by the blocker decision."""

    queries: tuple[RecordedQueryV1, ...]
    recent: RecentContractV1
    weekly: WeeklyContractV1
    detail: DetailContractV1


class QualificationBlockerV1(FrozenModel):
    """One completeness fact preventing live qualification."""

    code: Literal[
        "recent-window-fidelity-contradicted",
        "weekly-list-terminality-unproven",
        "older-open-inventory-unproven",
    ]
    explanation: str


class QualificationCheckV1(FrozenModel):
    """One acceptance check with no truthy shortcut for skipped work."""

    name: str
    status: Literal["passed", "failed", "not-run"]


class QualificationCostsV1(FrozenModel):
    """Live probe cost plus the guaranteed offline rerun cost."""

    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)
    offline_rerun_request_count: Literal[0] = 0


class PendingWeeklyCycleV1(FrozenModel):
    """One genuinely later operational cycle that has not occurred."""

    due_on: date
    status: Literal["pending"] = "pending"


class CheshireEastQualificationBlockerReceiptV1(FrozenModel):
    """Versioned artifact whose schema cannot represent qualification success."""

    schema_version: Literal[1] = 1
    kind: Literal["cheshire-east-qualification-blocker"] = (
        "cheshire-east-qualification-blocker"
    )
    authority_id: Literal["cheshire-east"] = "cheshire-east"
    outcome: Literal["blocked"] = "blocked"
    promotion_allowed: Literal[False] = False
    created_at: datetime
    scope: QualificationScopeV1
    query_inventory: tuple[str, ...]
    source_contract: SourceContractV1
    blockers: tuple[QualificationBlockerV1, ...] = Field(min_length=1)
    checks: tuple[QualificationCheckV1, ...]
    costs: QualificationCostsV1
    evidence: tuple[RetainedEvidenceV1, ...] = Field(min_length=1)
    operational_store: Literal["not-created"] = "not-created"
    weekly_cycles: tuple[PendingWeeklyCycleV1, PendingWeeklyCycleV1]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScopeV1
    resume: bool


class QualificationConfigError(ValueError):
    """One safety flag or scope value is invalid."""


class QualificationEvidenceError(RuntimeError):
    """A retained response no longer matches its receipt."""


class _ProbeResult(FrozenModel):
    source_contract: SourceContractV1
    captures: tuple[EvidenceCapture, ...]
    costs: QualificationCostsV1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    try:
        scope = QualificationScopeV1(start=start, end=end, include_open=True)
    except ValueError as error:
        raise QualificationConfigError(_EXACT_WINDOW_REQUIRED) from error
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and not data_dir.is_dir():
        raise QualificationConfigError(_DATA_DIR_NOT_DIRECTORY)
    if data_dir.exists() and any(data_dir.iterdir()) and not arguments.resume:
        raise QualificationConfigError(_RESUME_REQUIRED)
    return _Config(data_dir=data_dir, scope=scope, resume=arguments.resume)


async def _probe(scope: QualificationScopeV1, session: PortalSession) -> _ProbeResult:
    captures: list[EvidenceCapture] = []
    search_form_capture = await session.fetch(cheshire.search_form_request())
    captures.append(search_form_capture)
    search_form = cheshire.parse_search_form(search_form_capture.body)
    recent_request = cheshire.valid_date_request(
        search_form,
        DiscoveryWindow(
            start=scope.start,
            end=scope.end,
            include_open=scope.include_open,
        ),
    )
    recent_capture = await session.fetch(recent_request)
    captures.append(recent_capture)
    recent_boundary = cheshire.parse_search_boundary(recent_capture.body)

    weekly_form_capture = await session.fetch(cheshire.weekly_received_form_request())
    captures.append(weekly_form_capture)
    weekly_form = cheshire.parse_weekly_form(weekly_form_capture.body)
    weekly_request = cheshire.weekly_received_request(
        weekly_form,
        _HISTORICAL_WEEK,
    )
    weekly_capture = await session.fetch(weekly_request)
    captures.append(weekly_capture)
    weekly_boundary = cheshire.parse_weekly_boundary(weekly_capture.body)

    detail_portal_request = cheshire.detail_request(_DETAIL_LOCATOR)
    detail_capture = await session.fetch(detail_portal_request)
    captures.append(detail_capture)
    detail = cheshire.parse_detail_contract(
        detail_capture.body,
        expected_reference=_DETAIL_REFERENCE,
        expected_locator=_DETAIL_LOCATOR,
    )
    queries = (
        _recorded_query(
            f"recent|valid|{scope.start.isoformat()}|{scope.end.isoformat()}",
            recent_request,
        ),
        _recorded_query(
            f"older-open|weekly-received|{_HISTORICAL_WEEK.isoformat()}",
            weekly_request,
        ),
        _recorded_query(f"detail|{_DETAIL_LOCATOR}", detail_portal_request),
    )
    source_contract = SourceContractV1(
        queries=queries,
        recent=RecentContractV1(
            explicit_zero=recent_boundary.explicit_zero,
            visible_references=tuple(
                result.public_reference for result in recent_boundary.results
            ),
        ),
        weekly=WeeklyContractV1(
            week=_HISTORICAL_WEEK,
            row_count=len(weekly_boundary.rows),
            reported_total=weekly_boundary.reported_total,
            pagination_links=weekly_boundary.pagination_links,
            terminal_marker=weekly_boundary.terminal_marker,
        ),
        detail=DetailContractV1(
            public_reference=detail.public_reference,
            locator=_DETAIL_LOCATOR,
            application_status=detail.application_status,
            valid_date=detail.valid_date,
            document_count=len(detail.documents),
            documents=tuple(
                DocumentContractV1.model_validate(document.model_dump())
                for document in detail.documents
            ),
        ),
    )
    return _ProbeResult(
        source_contract=source_contract,
        captures=tuple(captures),
        costs=QualificationCostsV1(
            request_count=len(session.requested_urls),
            transferred_bytes=session.transferred_bytes,
            attachment_body_requests=session.attachment_body_requests,
        ),
    )


async def _run_probe(
    scope: QualificationScopeV1,
    session_factory: SessionFactory,
) -> _ProbeResult:
    session = session_factory()
    try:
        return await _probe(scope, session)
    finally:
        await session.aclose()


def _recorded_query(key: str, request: PortalRequest) -> RecordedQueryV1:
    return RecordedQueryV1(
        key=key,
        method=request.method,
        url=request.url,
        form=tuple(
            RecordedFieldV1(name=field.name, value=field.value)
            for field in request.form
        ),
    )


def _retain_evidence(
    store: EvidenceStore,
    captures: tuple[EvidenceCapture, ...],
) -> tuple[RetainedEvidenceV1, ...]:
    retained = []
    for capture in captures:
        path = store.put(capture)
        body = gzip.decompress(path.read_bytes())
        digest = sha256(body).hexdigest()
        if digest != capture.digest:
            raise QualificationEvidenceError
        retained.append(
            RetainedEvidenceV1(
                digest=digest,
                relative_path=store.relative_path(path),
                source_url=capture.url,
                media_type=capture.media_type,
                byte_count=len(body),
            )
        )
    return tuple(retained)


def _receipt(
    scope: QualificationScopeV1,
    probe: _ProbeResult,
    evidence: tuple[RetainedEvidenceV1, ...],
    created_at: datetime,
) -> CheshireEastQualificationBlockerReceiptV1:
    recent_contradicted = (
        probe.source_contract.detail.valid_date >= scope.start
        and probe.source_contract.detail.valid_date <= scope.end
        and probe.source_contract.detail.public_reference
        not in probe.source_contract.recent.visible_references
    )
    weekly_unproved = (
        probe.source_contract.weekly.reported_total is None
        and not probe.source_contract.weekly.pagination_links
        and not probe.source_contract.weekly.terminal_marker
    )
    blockers = []
    if recent_contradicted:
        blockers.append(
            QualificationBlockerV1(
                code="recent-window-fidelity-contradicted",
                explanation=(
                    "a direct detail valid inside the requested window was absent "
                    "from the valid-date result"
                ),
            )
        )
    if weekly_unproved:
        blockers.append(
            QualificationBlockerV1(
                code="weekly-list-terminality-unproven",
                explanation=(
                    "the historical weekly page published no total, pagination, "
                    "or terminal marker"
                ),
            )
        )
    blockers.append(
        QualificationBlockerV1(
            code="older-open-inventory-unproven",
            explanation=(
                "the official portal exposes no complete active-status query or "
                "proven exhaustive historical partition"
            ),
        )
    )
    return CheshireEastQualificationBlockerReceiptV1(
        created_at=created_at,
        scope=scope,
        query_inventory=tuple(query.key for query in probe.source_contract.queries),
        source_contract=probe.source_contract,
        blockers=tuple(blockers),
        checks=(
            QualificationCheckV1(name="exact-query-inventory", status="passed"),
            QualificationCheckV1(
                name="recent-window-fidelity",
                status="failed" if recent_contradicted else "passed",
            ),
            QualificationCheckV1(
                name="weekly-list-terminality",
                status="failed" if weekly_unproved else "passed",
            ),
            QualificationCheckV1(name="older-open-inventory", status="failed"),
            QualificationCheckV1(name="detail-and-documents", status="passed"),
            QualificationCheckV1(name="attachment-policy", status="passed"),
            QualificationCheckV1(name="source-evidence-integrity", status="passed"),
            QualificationCheckV1(name="sqlite-integrity", status="not-run"),
            QualificationCheckV1(name="durable-queue-agreement", status="not-run"),
        ),
        costs=probe.costs,
        evidence=evidence,
        weekly_cycles=(
            PendingWeeklyCycleV1(due_on=scope.end + timedelta(days=7)),
            PendingWeeklyCycleV1(due_on=scope.end + timedelta(days=14)),
        ),
    )


def _write_receipt(
    path: Path,
    receipt: CheshireEastQualificationBlockerReceiptV1,
) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    payload = f"{receipt.model_dump_json(indent=2)}\n"
    with temporary.open("w", encoding="utf-8") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_receipt(
    data_dir: Path,
    expected_scope: QualificationScopeV1,
) -> CheshireEastQualificationBlockerReceiptV1:
    receipt_path = data_dir / _RECEIPT_NAME
    if not receipt_path.is_file():
        raise QualificationConfigError(_RECEIPT_REQUIRED)
    receipt = CheshireEastQualificationBlockerReceiptV1.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    if receipt.scope != expected_scope:
        raise QualificationConfigError(_INVALID_WINDOW)
    evidence_root = (data_dir / "evidence").resolve(strict=True)
    for item in receipt.evidence:
        candidate = (evidence_root / item.relative_path).resolve(strict=True)
        if not candidate.is_relative_to(evidence_root):
            raise QualificationEvidenceError
        body = gzip.decompress(candidate.read_bytes())
        if len(body) != item.byte_count or sha256(body).hexdigest() != item.digest:
            raise QualificationEvidenceError
    return receipt


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
    """Capture or offline-verify the typed Cheshire East blocker."""
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except QualificationConfigError as error:
        return _error(str(error), 2)
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            if config.resume:
                receipt = _verify_receipt(config.data_dir, config.scope)
            else:
                probe = asyncio.run(_run_probe(config.scope, session_factory))
                evidence_store = EvidenceStore(config.data_dir / "evidence")
                evidence = _retain_evidence(evidence_store, probe.captures)
                receipt = _receipt(config.scope, probe, evidence, now())
                _write_receipt(config.data_dir / _RECEIPT_NAME, receipt)
    except Exception as error:  # noqa: BLE001
        return _error("runtime-failure", 1, exception=type(error).__name__)
    print(receipt.model_dump_json())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
