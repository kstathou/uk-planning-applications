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
from typing import Literal, Never, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, HttpUrl, ValidationError, model_validator

import yimby.authorities.cheshire_east.adapter as cheshire
from yimby.domain import DiscoveryWindow, EvidenceCapture, FrozenModel
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import CollectionAlreadyRunningError, ProcessLock
from yimby.transport import (
    PortalRequest,
    PortalSession,
    RequestMethod,
    SourceUnavailableError,
)

_RECEIPT_NAME = "cheshire-east-qualification-blocker-v2.json"
_DETAIL_REFERENCE = "26/3335/PRIOR-1A"
_DETAIL_LOCATOR = "406569"
_HISTORICAL_WEEK = date(2024, 1, 1)
_SEARCH_FORM_REQUEST_INDEX = 0
_RECENT_REQUEST_INDEX = 1
_WEEKLY_FORM_REQUEST_INDEX = 2
_WEEKLY_REQUEST_INDEX = 3
_DETAIL_REQUEST_INDEX = 4
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


def _raise_invariant(code: str) -> Never:
    raise ValueError(code)


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

    @model_validator(mode="after")
    def zero_agrees_with_references(self) -> Self:
        """Require exactly one of an explicit zero or visible result rows."""
        if self.explicit_zero == bool(self.visible_references):
            _raise_invariant("recent-result-mismatch")
        return self


class WeeklyContractV1(FrozenModel):
    """Observed historical weekly boundary without inferred terminality."""

    week: date
    row_count: int = Field(ge=0)
    reported_total: int | None
    pagination_links: tuple[str, ...]
    terminal_marker: bool

    @model_validator(mode="after")
    def total_covers_rows(self) -> Self:
        """Reject a published total smaller than the rows on the page."""
        if self.reported_total is not None and self.reported_total < self.row_count:
            _raise_invariant("weekly-total-mismatch")
        return self


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

    @model_validator(mode="after")
    def count_matches_documents(self) -> Self:
        """Require the published count to describe the retained metadata rows."""
        if self.document_count != len(self.documents):
            _raise_invariant("document-count-mismatch")
        for document in self.documents:
            parsed = urlsplit(str(document.url))
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "pa.cheshireeast.gov.uk"
                or parsed.path != "/planning/"
                or query.get("fa") != ["downloadDocument"]
                or query.get("public_record_id") != [self.locator]
                or len(query.get("id", ())) != 1
                or not query["id"][0].isdigit()
                or set(query) != {"fa", "id", "public_record_id"}
            ):
                _raise_invariant("document-url-mismatch")
        return self


class SourceContractV1(FrozenModel):
    """All official observations used by the blocker decision."""

    queries: tuple[RecordedQueryV1, ...]
    recent: RecentContractV1
    weekly: WeeklyContractV1
    detail: DetailContractV1


def _recent_window_contradicted(
    scope: QualificationScopeV1,
    source_contract: SourceContractV1,
) -> bool:
    return (
        scope.start <= source_contract.detail.valid_date <= scope.end
        and source_contract.detail.public_reference
        not in source_contract.recent.visible_references
    )


def _weekly_terminality_unproved(weekly: WeeklyContractV1) -> bool:
    published_count_agrees = (
        weekly.reported_total is None or weekly.reported_total == weekly.row_count
    )
    return not (
        not weekly.pagination_links
        and published_count_agrees
        and (weekly.terminal_marker or weekly.reported_total is not None)
    )


class QualificationBlockerV1(FrozenModel):
    """One completeness fact preventing live qualification."""

    code: Literal[
        "official-search-form-unavailable",
        "official-source-contract-drift",
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


class CheshireEastQualificationBlockerReceiptV2(FrozenModel):
    """Versioned artifact whose schema cannot represent qualification success."""

    schema_version: Literal[2] = 2
    kind: Literal["cheshire-east-qualification-blocker"] = (
        "cheshire-east-qualification-blocker"
    )
    authority_id: Literal["cheshire-east"] = "cheshire-east"
    outcome: Literal["blocked"] = "blocked"
    promotion_allowed: Literal[False] = False
    created_at: datetime
    scope: QualificationScopeV1
    query_inventory: tuple[str, ...]
    pending_query_inventory: tuple[str, ...]
    attempted_requests: tuple[RecordedQueryV1, ...] = Field(min_length=1)
    source_contract: SourceContractV1 | None
    blockers: tuple[QualificationBlockerV1, ...] = Field(min_length=1)
    checks: tuple[QualificationCheckV1, ...]
    costs: QualificationCostsV1
    evidence: tuple[RetainedEvidenceV1, ...] = Field(min_length=1)
    operational_store: Literal["not-created"] = "not-created"
    weekly_cycles: tuple[PendingWeeklyCycleV1, PendingWeeklyCycleV1]

    @model_validator(mode="after")
    def semantic_invariants(self) -> Self:
        """Reject receipts whose independently useful facts disagree."""
        planned = _planned_query_inventory(self.scope)
        attempted = tuple(request.key for request in self.attempted_requests)
        expected_pending = planned[len(attempted) :]
        if (
            attempted != planned[: len(attempted)]
            or self.query_inventory != attempted
            or self.pending_query_inventory != expected_pending
            or len(self.evidence) != len(attempted)
            or self.costs.request_count != len(attempted)
            or self.costs.attachment_body_requests != 0
            or self.costs.transferred_bytes
            != sum(item.byte_count for item in self.evidence)
            or self.weekly_cycles
            != (
                PendingWeeklyCycleV1(due_on=self.scope.end + timedelta(days=7)),
                PendingWeeklyCycleV1(due_on=self.scope.end + timedelta(days=14)),
            )
        ):
            _raise_invariant("receipt-invariant-mismatch")
        _validate_attempted_request_shapes(self.scope, self.attempted_requests)
        _validate_evidence_bindings(self.attempted_requests, self.evidence)

        checks = {check.name: check.status for check in self.checks}
        if len(checks) != len(self.checks):
            _raise_invariant("receipt-check-duplicate")
        expected_checks: dict[str, str] = {
            "exact-query-inventory": "passed",
            "attachment-policy": "passed",
            "source-evidence-integrity": "passed",
            "sqlite-integrity": "not-run",
            "durable-queue-agreement": "not-run",
        }
        if self.source_contract is None:
            if len(self.blockers) != 1 or self.blockers[0].code not in {
                "official-search-form-unavailable",
                "official-source-contract-drift",
            }:
                _raise_invariant("receipt-source-blocker-mismatch")
            expected_checks.update(
                {
                    "official-search-form": (
                        "failed"
                        if self.blockers[0].code == "official-search-form-unavailable"
                        else "passed"
                    ),
                    "source-contract": "failed",
                    "recent-window-fidelity": "not-run",
                    "weekly-list-terminality": "not-run",
                    "older-open-inventory": "not-run",
                    "detail-and-documents": "not-run",
                }
            )
        else:
            if (
                self.source_contract.weekly.week != _HISTORICAL_WEEK
                or self.source_contract.detail.locator != _DETAIL_LOCATOR
                or self.source_contract.detail.public_reference != _DETAIL_REFERENCE
            ):
                _raise_invariant("source-contract-identity-mismatch")
            recent_contradicted = _recent_window_contradicted(
                self.scope,
                self.source_contract,
            )
            weekly_unproved = _weekly_terminality_unproved(self.source_contract.weekly)
            expected_code_list = []
            if recent_contradicted:
                expected_code_list.append("recent-window-fidelity-contradicted")
            if weekly_unproved:
                expected_code_list.append("weekly-list-terminality-unproven")
            expected_code_list.append("older-open-inventory-unproven")
            expected_codes = tuple(expected_code_list)
            semantic_requests = tuple(
                request
                for request in self.attempted_requests
                if not request.key.startswith("source-access|")
            )
            if (
                attempted != planned
                or tuple(blocker.code for blocker in self.blockers) != expected_codes
                or self.source_contract.queries != semantic_requests
            ):
                _raise_invariant("receipt-source-contract-mismatch")
            expected_checks.update(
                {
                    "official-search-form": "passed",
                    "source-contract": "passed",
                    "recent-window-fidelity": (
                        "failed" if recent_contradicted else "passed"
                    ),
                    "weekly-list-terminality": (
                        "failed" if weekly_unproved else "passed"
                    ),
                    "older-open-inventory": "failed",
                    "detail-and-documents": "passed",
                }
            )
        if checks != expected_checks:
            _raise_invariant("receipt-check-mismatch")
        return self


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScopeV1
    resume: bool


class QualificationConfigError(ValueError):
    """One safety flag or scope value is invalid."""


class QualificationEvidenceError(RuntimeError):
    """A retained response no longer matches its receipt."""


class _ProbeResult(FrozenModel):
    source_contract: SourceContractV1 | None
    blocker: QualificationBlockerV1 | None
    attempted_requests: tuple[RecordedQueryV1, ...]
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
    attempted_requests: list[RecordedQueryV1] = []

    async def capture(key: str, request: PortalRequest) -> EvidenceCapture:
        attempted_requests.append(_recorded_query(key, request))
        response = await session.fetch(request)
        captures.append(response)
        return response

    search_form_capture = await capture(
        "source-access|search-form",
        cheshire.search_form_request(),
    )
    try:
        search_form = cheshire.parse_search_form(search_form_capture.body)
    except (
        cheshire.CheshireEastFormMethodUnavailableError,
        cheshire.CheshireEastParseError,
    ):
        return _probe_result(
            session,
            captures,
            attempted_requests,
            blocker=QualificationBlockerV1(
                code="official-search-form-unavailable",
                explanation=(
                    "the official HTTP response did not expose the recorded "
                    "search form, so no search or detail query was attempted"
                ),
            ),
        )
    try:
        recent_key = f"recent|valid|{scope.start.isoformat()}|{scope.end.isoformat()}"
        recent_request = cheshire.valid_date_request(
            search_form,
            DiscoveryWindow(
                start=scope.start,
                end=scope.end,
                include_open=scope.include_open,
            ),
        )
        recent_capture = await capture(recent_key, recent_request)
        recent_boundary = cheshire.parse_search_boundary(recent_capture.body)

        weekly_form_capture = await capture(
            "source-access|weekly-form",
            cheshire.weekly_received_form_request(),
        )
        weekly_form = cheshire.parse_weekly_form(weekly_form_capture.body)
        weekly_key = f"older-open|weekly-received|{_HISTORICAL_WEEK.isoformat()}"
        weekly_request = cheshire.weekly_received_request(
            weekly_form,
            _HISTORICAL_WEEK,
        )
        weekly_capture = await capture(weekly_key, weekly_request)
        weekly_boundary = cheshire.parse_weekly_boundary(weekly_capture.body)

        detail_key = f"detail|{_DETAIL_LOCATOR}"
        detail_portal_request = cheshire.detail_request(_DETAIL_LOCATOR)
        detail_capture = await capture(detail_key, detail_portal_request)
        detail = cheshire.parse_detail_contract(
            detail_capture.body,
            expected_reference=_DETAIL_REFERENCE,
            expected_locator=_DETAIL_LOCATOR,
        )
    except (
        cheshire.CheshireEastFormMethodUnavailableError,
        cheshire.CheshireEastParseError,
        cheshire.CheshireEastReferenceMismatchError,
    ):
        return _probe_result(
            session,
            captures,
            attempted_requests,
            blocker=QualificationBlockerV1(
                code="official-source-contract-drift",
                explanation=(
                    "an official response no longer matched the recorded source "
                    "contract; every completed response was retained"
                ),
            ),
        )
    queries = (
        _recorded_query(recent_key, recent_request),
        _recorded_query(weekly_key, weekly_request),
        _recorded_query(detail_key, detail_portal_request),
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
    return _probe_result(
        session,
        captures,
        attempted_requests,
        source_contract=source_contract,
    )


def _probe_result(
    session: PortalSession,
    captures: list[EvidenceCapture],
    attempted_requests: list[RecordedQueryV1],
    *,
    source_contract: SourceContractV1 | None = None,
    blocker: QualificationBlockerV1 | None = None,
) -> _ProbeResult:
    return _ProbeResult(
        source_contract=source_contract,
        blocker=blocker,
        attempted_requests=tuple(attempted_requests),
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


def _planned_query_inventory(scope: QualificationScopeV1) -> tuple[str, ...]:
    return (
        "source-access|search-form",
        f"recent|valid|{scope.start.isoformat()}|{scope.end.isoformat()}",
        "source-access|weekly-form",
        f"older-open|weekly-received|{_HISTORICAL_WEEK.isoformat()}",
        f"detail|{_DETAIL_LOCATOR}",
    )


def _validate_attempted_request_shapes(
    scope: QualificationScopeV1,
    requests: tuple[RecordedQueryV1, ...],
) -> None:
    static_requests = {
        _SEARCH_FORM_REQUEST_INDEX: _recorded_query(
            "source-access|search-form",
            cheshire.search_form_request(),
        ),
        _WEEKLY_FORM_REQUEST_INDEX: _recorded_query(
            "source-access|weekly-form",
            cheshire.weekly_received_form_request(),
        ),
        _DETAIL_REQUEST_INDEX: _recorded_query(
            f"detail|{_DETAIL_LOCATOR}",
            cheshire.detail_request(_DETAIL_LOCATOR),
        ),
    }
    for index, expected in static_requests.items():
        if len(requests) > index and requests[index] != expected:
            _raise_invariant("request-shape-mismatch")

    if len(requests) > _RECENT_REQUEST_INDEX:
        recent = requests[_RECENT_REQUEST_INDEX]
        fields = _unique_recorded_fields(recent.form)
        if (
            recent.method != RequestMethod.POST
            or str(recent.url) != "https://pa.cheshireeast.gov.uk/planning/index.html"
            or fields.get("fa") != "search"
            or fields.get("valid_date_from") != scope.start.strftime("%d-%m-%Y")
            or fields.get("valid_date_to") != scope.end.strftime("%d-%m-%Y")
        ):
            _raise_invariant("request-shape-mismatch")

    if len(requests) > _WEEKLY_REQUEST_INDEX:
        weekly = requests[_WEEKLY_REQUEST_INDEX]
        fields = _unique_recorded_fields(weekly.form)
        if (
            weekly.method != RequestMethod.POST
            or str(weekly.url)
            != (
                "https://pa.cheshireeast.gov.uk/planning/"
                "index.html?fa=getReceivedWeeklyList"
            )
            or fields != {"week": "01-01-2024", "fa": ""}
        ):
            _raise_invariant("request-shape-mismatch")


def _unique_recorded_fields(
    fields: tuple[RecordedFieldV1, ...],
) -> dict[str, str]:
    result = {field.name: field.value for field in fields}
    if len(result) != len(fields):
        _raise_invariant("request-form-duplicate")
    return result


def _validate_evidence_bindings(
    requests: tuple[RecordedQueryV1, ...],
    evidence: tuple[RetainedEvidenceV1, ...],
) -> None:
    for request, item in zip(requests, evidence, strict=True):
        request_url = urlsplit(str(request.url))
        evidence_url = urlsplit(str(item.source_url))
        if (
            evidence_url.scheme != request_url.scheme
            or evidence_url.netloc != request_url.netloc
            or evidence_url.path != request_url.path
            or evidence_url.query not in {"", request_url.query}
            or item.media_type != "text/html"
        ):
            _raise_invariant("request-evidence-mismatch")


def _verify_request_evidence_contract(
    receipt: CheshireEastQualificationBlockerReceiptV2,
    bodies: tuple[bytes, ...],
) -> None:
    requests = receipt.attempted_requests
    if len(requests) > _RECENT_REQUEST_INDEX:
        search_form = cheshire.parse_search_form(bodies[_SEARCH_FORM_REQUEST_INDEX])
        expected_recent = _recorded_query(
            requests[_RECENT_REQUEST_INDEX].key,
            cheshire.valid_date_request(
                search_form,
                DiscoveryWindow(
                    start=receipt.scope.start,
                    end=receipt.scope.end,
                    include_open=True,
                ),
            ),
        )
        if requests[_RECENT_REQUEST_INDEX] != expected_recent:
            raise QualificationEvidenceError
    if len(requests) > _WEEKLY_REQUEST_INDEX:
        weekly_form = cheshire.parse_weekly_form(bodies[_WEEKLY_FORM_REQUEST_INDEX])
        expected_weekly = _recorded_query(
            requests[_WEEKLY_REQUEST_INDEX].key,
            cheshire.weekly_received_request(weekly_form, _HISTORICAL_WEEK),
        )
        if requests[_WEEKLY_REQUEST_INDEX] != expected_weekly:
            raise QualificationEvidenceError


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
) -> CheshireEastQualificationBlockerReceiptV2:
    query_inventory = tuple(request.key for request in probe.attempted_requests)
    pending_queries = tuple(
        key for key in _planned_query_inventory(scope) if key not in query_inventory
    )
    if probe.source_contract is None:
        if probe.blocker is None:
            message = "blocked probe requires a typed blocker"
            raise ValueError(message)
        return CheshireEastQualificationBlockerReceiptV2(
            created_at=created_at,
            scope=scope,
            query_inventory=query_inventory,
            pending_query_inventory=pending_queries,
            attempted_requests=probe.attempted_requests,
            source_contract=None,
            blockers=(probe.blocker,),
            checks=(
                QualificationCheckV1(
                    name="official-search-form",
                    status=(
                        "failed"
                        if probe.blocker.code == "official-search-form-unavailable"
                        else "passed"
                    ),
                ),
                QualificationCheckV1(name="source-contract", status="failed"),
                QualificationCheckV1(name="exact-query-inventory", status="passed"),
                QualificationCheckV1(name="recent-window-fidelity", status="not-run"),
                QualificationCheckV1(name="weekly-list-terminality", status="not-run"),
                QualificationCheckV1(name="older-open-inventory", status="not-run"),
                QualificationCheckV1(name="detail-and-documents", status="not-run"),
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
    recent_contradicted = _recent_window_contradicted(
        scope,
        probe.source_contract,
    )
    weekly_unproved = _weekly_terminality_unproved(probe.source_contract.weekly)
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
                    "the historical weekly page does not publish an internally "
                    "consistent terminal boundary"
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
    return CheshireEastQualificationBlockerReceiptV2(
        created_at=created_at,
        scope=scope,
        query_inventory=query_inventory,
        pending_query_inventory=(),
        attempted_requests=probe.attempted_requests,
        source_contract=probe.source_contract,
        blockers=tuple(blockers),
        checks=(
            QualificationCheckV1(name="official-search-form", status="passed"),
            QualificationCheckV1(name="source-contract", status="passed"),
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
    receipt: CheshireEastQualificationBlockerReceiptV2,
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
) -> CheshireEastQualificationBlockerReceiptV2:
    receipt_path = data_dir / _RECEIPT_NAME
    if not receipt_path.is_file():
        raise QualificationConfigError(_RECEIPT_REQUIRED)
    receipt = CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    if receipt.scope != expected_scope:
        raise QualificationConfigError(_INVALID_WINDOW)
    evidence_root = (data_dir / "evidence").resolve(strict=True)
    bodies = []
    for item in receipt.evidence:
        candidate = (evidence_root / item.relative_path).resolve(strict=True)
        if not candidate.is_relative_to(evidence_root):
            raise QualificationEvidenceError
        body = gzip.decompress(candidate.read_bytes())
        if len(body) != item.byte_count or sha256(body).hexdigest() != item.digest:
            raise QualificationEvidenceError
        bodies.append(body)
    _verify_request_evidence_contract(receipt, tuple(bodies))
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
            receipt_path = config.data_dir / _RECEIPT_NAME
            if config.resume and receipt_path.is_file():
                receipt = _verify_receipt(config.data_dir, config.scope)
            else:
                probe = asyncio.run(_run_probe(config.scope, session_factory))
                evidence_store = EvidenceStore(config.data_dir / "evidence")
                evidence = _retain_evidence(evidence_store, probe.captures)
                receipt = _receipt(config.scope, probe, evidence, now())
                _write_receipt(receipt_path, receipt)
    except (
        EOFError,
        OSError,
        ValidationError,
        cheshire.CheshireEastFormMethodUnavailableError,
        cheshire.CheshireEastParseError,
        cheshire.CheshireEastReferenceMismatchError,
        CollectionAlreadyRunningError,
        QualificationConfigError,
        QualificationEvidenceError,
        SourceUnavailableError,
    ) as error:
        return _error(
            "runtime-failure",
            1,
            exception=type(error).__name__,
            error_code=getattr(error, "code", "unclassified"),
        )
    print(receipt.model_dump_json())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
