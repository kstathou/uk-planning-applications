# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Persist a fail-closed qualification of Birmingham's ArcGIS evidence."""

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
from typing import Any, Literal, NoReturn, cast
from urllib.parse import urlencode

from pydantic import Field, HttpUrl

from yimby.authorities.birmingham.adapter import ARCGIS_LAYER_URL
from yimby.domain import (
    AuthorityId,
    EvidenceCapture,
    EvidenceDigest,
    FrozenModel,
    LiveReadiness,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HttpxPortalSession
from yimby.orchestration import ProcessLock
from yimby.registry import pilot_registry
from yimby.store import SqliteStore
from yimby.transport import PortalRequest, PortalSession, RequestIntent

_AUTHORITY_ID = AuthorityId("birmingham")
_RECEIPT_NAME = "birmingham-qualification-v1.json"
_PAGE_SIZE = 25
_MAX_PAGES = 1000
_RECENCY_MAX_LAG_DAYS = 1
_LAYER_ID = 12
_LAYER_MAX_RECORD_COUNT = 1000
_OBSERVED_RECORD_COUNT = 254_064
_OBSERVED_VOLUME_COUNTS = (5802, 453, 121)
_OBSERVED_NULL_DECISIONS = 3276
_OBSERVED_UNRESOLVED_CANDIDATES = 1419
_QUALIFICATION_START = date(2026, 8, 18)
_QUALIFICATION_END = date(2026, 9, 16)
_OBSERVED_FIELDS = (
    "OBJECTID",
    "SHAPE",
    "REFERENCE",
    "application_type_code",
    "TYPE",
    "Stat_Return_Code",
    "Sub_Cat",
    "Received",
    "LOCATION",
    "Dev",
    "Date_Accepted",
    "AGENT",
    "Decision_Level",
    "APPLICATION_DECISION",
    "Decision_Date",
    "Date_Issued",
    "APPEAL_DECISION",
    "Appeal_Decision_Date",
    "Officer",
    "PGP_PK",
    "PA_NO",
    "Number",
    "TypeOfObj",
    "SHAPE_Length",
    "SHAPE_Area",
)
_RECENT_WHERE = (
    "Received >= DATE '2026-08-18 00:00:00' AND Received < DATE '2026-09-17 00:00:00'"
)
_COUNT_2025_WHERE = (
    "Received >= DATE '2025-01-01 00:00:00' AND Received < DATE '2026-01-01 00:00:00'"
)
_COUNT_2026_JULY_WHERE = (
    "Received >= DATE '2026-07-01 00:00:00' AND Received < DATE '2026-08-01 00:00:00'"
)
_COUNT_2026_AUGUST_WHERE = (
    "Received >= DATE '2026-08-01 00:00:00' AND Received < DATE '2026-09-01 00:00:00'"
)
_DECISION_DATE_MISMATCH_WHERE = (
    "(APPLICATION_DECISION IS NULL AND Decision_Date IS NOT NULL) OR "
    "(APPLICATION_DECISION IS NOT NULL AND Decision_Date IS NULL)"
)
_UNRESOLVED_WHERE = (
    "APPLICATION_DECISION IS NULL AND Decision_Date IS NULL AND Date_Issued IS NULL"
)

SessionFactory = Callable[[], PortalSession]
Clock = Callable[[], datetime]


class QualificationConfigError(ValueError):
    """A required live-safety option is invalid."""


class QualificationInvariantError(RuntimeError):
    """The observations cannot support the blocked receipt contract."""


def _fail_config(code: str) -> NoReturn:
    raise QualificationConfigError(code)


def _fail_invariant(code: str) -> NoReturn:
    raise QualificationInvariantError(code)


class QualificationScopeV1(FrozenModel):
    """The exact inclusive window assessed by this command."""

    start: date
    end: date
    include_open: Literal[True] = True
    inclusive_days: Literal[30] = 30


class EvidenceReferenceV1(FrozenModel):
    """One content-addressed raw ArcGIS response."""

    sha256: str
    stored_path: str
    media_type: str
    byte_count: int = Field(ge=0)


class QueryObservationV1(FrozenModel):
    """One exact query and its retained response."""

    name: str
    url: str
    evidence: EvidenceReferenceV1


class RecentDiscoveryV1(FrozenModel):
    """Internally reconciled recent-window result."""

    status: Literal["proven"] = "proven"
    reported_count: int
    page_feature_count: int
    unique_reference_count: int
    unique_objectid_count: int
    count_page_reference_agreement: Literal[True] = True
    page_count: int
    terminal_page_observed: Literal[True] = True
    latest_reference: str
    latest_received: date


class SourceFreshnessV1(FrozenModel):
    """Fresh latest record, without inferring continuity from sample volumes."""

    status: Literal["not-proven"] = "not-proven"
    latest_received: date
    latest_accepted: date
    latest_record_is_current: Literal[True] = True
    future_accepted_date_present: Literal[True] = True
    volume_continuity: Literal["not-proven"] = "not-proven"
    calendar_2025_count: int
    july_2026_count: int
    august_2026_count: int
    rolling_30_day_count: int
    reason: Literal["unexplained-recent-volume-decline"] = (
        "unexplained-recent-volume-decline"
    )


class OlderOpenV1(FrozenModel):
    """Observed older-record ambiguity that blocks completeness."""

    status: Literal["not-proven"] = "not-proven"
    reason: Literal["no-explicit-case-status-field"] = "no-explicit-case-status-field"
    application_decision_null_count: int
    decision_date_mismatch_count: int
    unresolved_candidate_count: int
    contradictions: tuple[str, ...]


class ActiveAppealsV1(FrozenModel):
    """Current appeal membership is not represented by the layer."""

    status: Literal["not-proven"] = "not-proven"
    reason: Literal["no-appeal-lodged-or-current-status-field"] = (
        "no-appeal-lodged-or-current-status-field"
    )


class SectionGapV1(FrozenModel):
    """A required child section absent from the ArcGIS contract."""

    status: Literal["not-exposed"] = "not-exposed"


class SectionGapsV1(FrozenModel):
    """All required child surfaces absent from this layer."""

    documents: SectionGapV1
    comments: SectionGapV1
    conditions: SectionGapV1
    consultations: SectionGapV1
    relationships: SectionGapV1


class TransportCostV1(FrozenModel):
    """Network work performed by the initial evidence pass."""

    request_count: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class LocalIntegrityV1(FrozenModel):
    """Local stores verified before the receipt is published."""

    sqlite_integrity: Literal["ok"] = "ok"
    evidence_integrity: Literal["verified"] = "verified"
    evidence_count: int = Field(ge=0)


class OfflineReplayV1(FrozenModel):
    """A replay that has no transport dependency by construction."""

    request_count: Literal[0] = 0
    sqlite_integrity: Literal["ok"] = "ok"
    evidence_integrity: Literal["verified"] = "verified"


class RegistryReadinessV1(FrozenModel):
    """Proof that qualification did not promote Birmingham."""

    before: Literal["blocked"] = "blocked"
    after: Literal["blocked"] = "blocked"
    promotion_attempted: Literal[False] = False


class BirminghamArcgisCheckpointV1(FrozenModel):
    """Terminal recent-window pagination state."""

    schema_version: Literal[1] = 1
    page_size: Literal[25] = 25
    next_offset: int = Field(ge=0)
    terminal: Literal[True] = True
    reported_count: int = Field(ge=0)
    collected_count: int = Field(ge=0)


class PendingWeeklyCycleV1(FrozenModel):
    """A later operational cycle that a blocked bootstrap cannot schedule."""

    ordinal: Literal[1, 2]
    required_offset_days: Literal[7, 14]
    relative_to: Literal["successful-live-bootstrap"] = "successful-live-bootstrap"
    status: Literal["pending"] = "pending"
    scheduled_at: None = None
    completed_at: None = None


class BirminghamBlockedQualificationReceiptV1(FrozenModel):
    """Versioned proof of a successful fail-closed assessment."""

    schema_version: Literal[1] = 1
    authority_id: Literal["birmingham"] = "birmingham"
    outcome: Literal["blocked"] = "blocked"
    created_at: datetime
    scope: QualificationScopeV1
    source_freshness: SourceFreshnessV1
    recent_discovery: RecentDiscoveryV1
    older_open: OlderOpenV1
    active_appeals: ActiveAppealsV1
    sections: SectionGapsV1
    query_inventory: tuple[QueryObservationV1, ...]
    transport: TransportCostV1
    local_integrity: LocalIntegrityV1
    offline_replay: OfflineReplayV1
    registry_readiness: RegistryReadinessV1
    checkpoint: BirminghamArcgisCheckpointV1
    weekly_cycles: tuple[PendingWeeklyCycleV1, PendingWeeklyCycleV1]


class _Config(FrozenModel):
    data_dir: Path
    scope: QualificationScopeV1


class _SourceFacts(FrozenModel):
    latest_received: date
    latest_accepted: date
    count_2025: int
    count_2026_july: int
    count_2026_august: int


class _RecentFacts(FrozenModel):
    reported_count: int
    feature_count: int
    unique_reference_count: int
    unique_objectid_count: int
    page_count: int
    latest_reference: str
    latest_received: date
    next_offset: int


class _OlderOpenFacts(FrozenModel):
    application_decision_null_count: int
    decision_date_mismatch_count: int
    unresolved_candidate_count: int


class _ReplaySession:
    """Serve retained captures in canonical query order without a transport."""

    def __init__(
        self,
        data_dir: Path,
        inventory: Sequence[QueryObservationV1],
    ) -> None:
        evidence = EvidenceStore(data_dir / "evidence")
        self._items = tuple(inventory)
        self._captures = tuple(
            evidence.read_capture(
                EvidenceDigest(item.evidence.sha256),
                item.evidence.stored_path,
                item.url,
                item.evidence.media_type,
            )
            for item in inventory
        )
        self._requested_urls: list[str] = []

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        index = len(self._requested_urls)
        if index >= len(self._items) or str(request.url) != self._items[index].url:
            _fail_invariant("offline-replay-query-mismatch")
        self._requested_urls.append(str(request.url))
        return self._captures[index]

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(self._requested_urls)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return sum(len(capture.body) for capture in self._captures)

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.NOT_RUN

    async def aclose(self) -> None:
        """Match the portal-session lifecycle without opening a resource."""

    @property
    def complete(self) -> bool:
        return len(self._requested_urls) == len(self._items)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist Birmingham ArcGIS qualification evidence.",
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--include-open", action="store_true")
    return parser


def _config(argv: Sequence[str]) -> _Config:
    arguments = _parser().parse_args(argv)
    if not arguments.confirm_live:
        _fail_config("confirmation-required")
    if not arguments.include_open:
        _fail_config("include-open-required")
    try:
        start = date.fromisoformat(arguments.start)
        end = date.fromisoformat(arguments.end)
    except ValueError as error:
        message = "invalid-date"
        raise QualificationConfigError(message) from error
    if (start, end) != (_QUALIFICATION_START, _QUALIFICATION_END):
        _fail_config("exact-30-day-window-required")
    data_dir = Path(arguments.data_dir).expanduser()
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        _fail_config("data-dir-not-empty")
    return _Config(
        data_dir=data_dir,
        scope=QualificationScopeV1(start=start, end=end),
    )


def _query_url(parameters: Sequence[tuple[str, str]]) -> str:
    return f"{ARCGIS_LAYER_URL}/query?{urlencode(parameters)}"


def _count_url(where: str) -> str:
    return _query_url((("f", "json"), ("where", where), ("returnCountOnly", "true")))


def _statistics(expressions: Sequence[tuple[str, str, str]]) -> str:
    return json.dumps(
        [
            {
                "statisticType": statistic,
                "onStatisticField": field,
                "outStatisticFieldName": output,
            }
            for statistic, field, output in expressions
        ],
        separators=(",", ":"),
    )


def _profile_url() -> str:
    return _query_url(
        (
            ("f", "json"),
            ("where", "1=1"),
            (
                "outStatistics",
                _statistics(
                    (
                        ("count", "OBJECTID", "record_count"),
                        ("max", "OBJECTID", "max_objectid"),
                        ("max", "Received", "latest_received"),
                        ("max", "Date_Accepted", "latest_accepted"),
                    )
                ),
            ),
            ("returnGeometry", "false"),
        )
    )


def _page_url(offset: int) -> str:
    return _query_url(
        (
            ("f", "json"),
            ("where", _RECENT_WHERE),
            ("outFields", "OBJECTID,REFERENCE,Received"),
            ("orderByFields", "OBJECTID ASC"),
            ("resultOffset", str(offset)),
            ("resultRecordCount", str(_PAGE_SIZE)),
            ("returnGeometry", "false"),
        )
    )


def _decision_groups_url() -> str:
    return _query_url(
        (
            ("f", "json"),
            ("where", "1=1"),
            ("groupByFieldsForStatistics", "APPLICATION_DECISION"),
            ("outFields", "APPLICATION_DECISION"),
            (
                "outStatistics",
                _statistics(
                    (
                        ("count", "OBJECTID", "record_count"),
                        ("min", "Received", "earliest_received"),
                        ("max", "Received", "latest_received"),
                    )
                ),
            ),
            ("returnGeometry", "false"),
        )
    )


def _sample_url(where: str, *, count: int = 10) -> str:
    return _query_url(
        (
            ("f", "json"),
            ("where", where),
            (
                "outFields",
                (
                    "REFERENCE,TYPE,Received,APPLICATION_DECISION,Decision_Date,"
                    "Date_Issued,APPEAL_DECISION"
                ),
            ),
            ("orderByFields", "Received ASC,OBJECTID ASC"),
            ("resultRecordCount", str(count)),
            ("returnGeometry", "false"),
        )
    )


def _unresolved_profile_url() -> str:
    return _query_url(
        (
            ("f", "json"),
            ("where", _UNRESOLVED_WHERE),
            (
                "outStatistics",
                _statistics(
                    (
                        ("count", "OBJECTID", "record_count"),
                        ("min", "Received", "earliest_received"),
                        ("max", "Received", "latest_received"),
                    )
                ),
            ),
            ("returnGeometry", "false"),
        )
    )


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = "invalid-json-response"
        raise QualificationInvariantError(message) from error
    if not isinstance(value, dict):
        _fail_invariant("unexpected-json-shape")
    return cast("dict[str, Any]", value)


def _integer(value: dict[str, Any], key: str) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        _fail_invariant(f"invalid-{key}")
    return candidate


def _attributes(feature: object) -> dict[str, Any]:
    if not isinstance(feature, dict):
        _fail_invariant("invalid-feature")
    attributes = feature.get("attributes")
    if not isinstance(attributes, dict):
        _fail_invariant("invalid-feature-attributes")
    return cast("dict[str, Any]", attributes)


def _features(value: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = value.get("features")
    if not isinstance(candidates, list):
        _fail_invariant("invalid-features")
    return [_attributes(candidate) for candidate in candidates]


def _first_attributes(value: dict[str, Any]) -> dict[str, Any]:
    features = _features(value)
    if len(features) != 1:
        _fail_invariant("expected-one-statistics-row")
    return features[0]


def _validate_schema(metadata: dict[str, Any]) -> None:
    fields = metadata.get("fields")
    if not isinstance(fields, list):
        _fail_invariant("layer-schema-mismatch")
    names = tuple(
        field.get("name") if isinstance(field, dict) else None for field in fields
    )
    capabilities = metadata.get("advancedQueryCapabilities")
    required_capabilities = (
        "supportsPagination",
        "supportsStatistics",
        "supportsOrderBy",
        "supportsDistinct",
    )
    if (
        metadata.get("id") != _LAYER_ID
        or metadata.get("name") != "Post 1990 Planning Application"
        or metadata.get("type") != "Feature Layer"
        or metadata.get("maxRecordCount") != _LAYER_MAX_RECORD_COUNT
        or metadata.get("hasAttachments") is not False
        or metadata.get("relationships") != []
        or names != _OBSERVED_FIELDS
        or not isinstance(capabilities, dict)
        or any(capabilities.get(name) is not True for name in required_capabilities)
    ):
        _fail_invariant("layer-schema-mismatch")


async def _observe(
    name: str,
    url: str,
    session: PortalSession,
    evidence_store: EvidenceStore,
    inventory: list[QueryObservationV1],
) -> dict[str, Any]:
    capture = await session.fetch(
        PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
    )
    stored = evidence_store.put(capture)
    inventory.append(
        QueryObservationV1(
            name=name,
            url=url,
            evidence=EvidenceReferenceV1(
                sha256=str(capture.digest),
                stored_path=evidence_store.relative_path(stored),
                media_type=capture.media_type,
                byte_count=len(capture.body),
            ),
        )
    )
    return _json_object(capture.body)


def _epoch_date(value: object) -> date:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail_invariant("invalid-arcgis-date")
    return datetime.fromtimestamp(value / 1000, tz=UTC).date()


def _statistics_date_range(
    attributes: dict[str, Any],
    latest_received: date,
) -> tuple[date, date]:
    earliest = _epoch_date(attributes.get("earliest_received"))
    latest = _epoch_date(attributes.get("latest_received"))
    if earliest > latest or latest > latest_received:
        _fail_invariant("historical-date-range-mismatch")
    return earliest, latest


def _historical_sample_dates(
    sample: Sequence[dict[str, Any]],
    latest_received: date,
    *,
    unresolved: bool,
) -> tuple[date, ...]:
    if not sample:
        _fail_invariant("missing-historical-sample")
    received_dates: list[date] = []
    for attributes in sample:
        reference = attributes.get("REFERENCE")
        application_type = attributes.get("TYPE")
        appeal_decision = attributes.get("APPEAL_DECISION")
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or not isinstance(application_type, str)
            or not application_type.strip()
            or attributes.get("APPLICATION_DECISION") is not None
            or (
                appeal_decision is not None
                and (
                    not isinstance(appeal_decision, str) or not appeal_decision.strip()
                )
            )
        ):
            _fail_invariant("historical-sample-predicate-mismatch")
        received = _epoch_date(attributes.get("Received"))
        if received >= _QUALIFICATION_START or received > latest_received:
            _fail_invariant("historical-sample-date-mismatch")
        for field in ("Decision_Date", "Date_Issued"):
            value = attributes.get(field)
            if value is not None:
                _epoch_date(value)
        if unresolved and (
            attributes.get("Decision_Date") is not None
            or attributes.get("Date_Issued") is not None
        ):
            _fail_invariant("historical-sample-predicate-mismatch")
        received_dates.append(received)
    if received_dates != sorted(received_dates):
        _fail_invariant("historical-sample-order-mismatch")
    return tuple(received_dates)


def _verify_evidence(
    data_dir: Path,
    inventory: Sequence[QueryObservationV1],
) -> bool:
    store = EvidenceStore(data_dir / "evidence")
    try:
        for item in inventory:
            capture = store.read_capture(
                EvidenceDigest(item.evidence.sha256),
                item.evidence.stored_path,
                item.url,
                item.evidence.media_type,
            )
            if (
                len(capture.body) != item.evidence.byte_count
                or sha256(capture.body).hexdigest() != item.evidence.sha256
            ):
                return False
    except (OSError, ValueError):
        return False
    return True


def _offline_result(
    data_dir: Path,
    inventory: Sequence[QueryObservationV1],
    sqlite_integrity: str,
) -> OfflineReplayV1:
    if sqlite_integrity != "ok" or not _verify_evidence(data_dir, inventory):
        _fail_invariant("local-integrity-failed")
    return OfflineReplayV1()


async def _source_facts(
    session: PortalSession,
    evidence_store: EvidenceStore,
    inventory: list[QueryObservationV1],
) -> _SourceFacts:
    metadata = await _observe(
        "layer-metadata",
        f"{ARCGIS_LAYER_URL}?f=json",
        session,
        evidence_store,
        inventory,
    )
    _validate_schema(metadata)
    profile = _first_attributes(
        await _observe(
            "layer-profile",
            _profile_url(),
            session,
            evidence_store,
            inventory,
        )
    )
    if (
        _integer(profile, "record_count") != _OBSERVED_RECORD_COUNT
        or _integer(profile, "max_objectid") != _OBSERVED_RECORD_COUNT
    ):
        _fail_invariant("layer-profile-mismatch")
    observed_counts: list[int] = []
    for name, where in (
        ("count-2025", _COUNT_2025_WHERE),
        ("count-2026-july", _COUNT_2026_JULY_WHERE),
        ("count-2026-august", _COUNT_2026_AUGUST_WHERE),
    ):
        observed_counts.append(
            _integer(
                await _observe(
                    name,
                    _count_url(where),
                    session,
                    evidence_store,
                    inventory,
                ),
                "count",
            )
        )
    counts = tuple(observed_counts)
    if counts != _OBSERVED_VOLUME_COUNTS:
        _fail_invariant("source-volume-observation-mismatch")
    latest_accepted = _epoch_date(profile.get("latest_accepted"))
    if latest_accepted <= _QUALIFICATION_END:
        _fail_invariant("future-accepted-date-not-observed")
    return _SourceFacts(
        latest_received=_epoch_date(profile.get("latest_received")),
        latest_accepted=latest_accepted,
        count_2025=counts[0],
        count_2026_july=counts[1],
        count_2026_august=counts[2],
    )


async def _recent_facts(
    latest_received: date,
    session: PortalSession,
    evidence_store: EvidenceStore,
    inventory: list[QueryObservationV1],
) -> _RecentFacts:
    reported_count = _integer(
        await _observe(
            "recent-count",
            _count_url(_RECENT_WHERE),
            session,
            evidence_store,
            inventory,
        ),
        "count",
    )
    recent: list[dict[str, Any]] = []
    offset = 0
    terminal = False
    page_count = 0
    while not terminal:
        page = await _observe(
            f"recent-page-{offset}",
            _page_url(offset),
            session,
            evidence_store,
            inventory,
        )
        page_features = _features(page)
        if len(page_features) > _PAGE_SIZE:
            _fail_invariant("page-size-exceeded")
        recent.extend(page_features)
        terminal = _page_is_terminal(
            page.get("exceededTransferLimit"),
            len(recent),
            reported_count,
        )
        offset += _PAGE_SIZE
        page_count += 1
        if page_count > _MAX_PAGES:
            _fail_invariant("pagination-not-bounded")

    references: list[str] = []
    object_ids: list[int] = []
    received: list[tuple[date, str]] = []
    for attributes in recent:
        reference = attributes.get("REFERENCE")
        object_id = attributes.get("OBJECTID")
        if (
            not isinstance(reference, str)
            or isinstance(object_id, bool)
            or not isinstance(object_id, int)
        ):
            _fail_invariant("invalid-recent-feature")
        received_date = _epoch_date(attributes.get("Received"))
        if not _QUALIFICATION_START <= received_date <= _QUALIFICATION_END:
            _fail_invariant("recent-feature-outside-window")
        references.append(reference)
        object_ids.append(object_id)
        received.append((received_date, reference))
    coherent = (
        reported_count == len(recent) == len(set(references)) == len(set(object_ids))
        and terminal
        and bool(recent)
        and object_ids == sorted(object_ids)
    )
    if not coherent:
        _fail_invariant("recent-count-page-reference-mismatch")
    latest_feature_date, latest_reference = max(received)
    if latest_feature_date != latest_received:
        _fail_invariant("latest-record-mismatch")
    recency_threshold = _QUALIFICATION_END - timedelta(days=_RECENCY_MAX_LAG_DAYS)
    if latest_feature_date < recency_threshold:
        _fail_invariant("latest-record-not-current")
    return _RecentFacts(
        reported_count=reported_count,
        feature_count=len(recent),
        unique_reference_count=len(set(references)),
        unique_objectid_count=len(set(object_ids)),
        page_count=page_count,
        latest_reference=latest_reference,
        latest_received=latest_received,
        next_offset=offset,
    )


def _page_is_terminal(
    exceeded_transfer_limit: object,
    collected_count: int,
    reported_count: int,
) -> bool:
    if isinstance(exceeded_transfer_limit, bool):
        return not exceeded_transfer_limit
    if exceeded_transfer_limit is None and collected_count == reported_count:
        return True
    _fail_invariant("missing-page-terminal-state")


async def _older_open_facts(
    latest_received: date,
    session: PortalSession,
    evidence_store: EvidenceStore,
    inventory: list[QueryObservationV1],
) -> _OlderOpenFacts:
    decision_groups = _features(
        await _observe(
            "application-decision-groups",
            _decision_groups_url(),
            session,
            evidence_store,
            inventory,
        )
    )
    null_group: dict[str, Any] | None = None
    for group in decision_groups:
        decision = group.get("APPLICATION_DECISION")
        if decision is not None and (
            not isinstance(decision, str) or not decision.strip()
        ):
            _fail_invariant("invalid-application-decision-group")
        if _integer(group, "record_count") <= 0:
            _fail_invariant("invalid-application-decision-group")
        _statistics_date_range(group, latest_received)
        if decision is None:
            if null_group is not None:
                _fail_invariant("multiple-null-decision-groups")
            null_group = group
    if null_group is None:
        _fail_invariant("missing-null-decision-group")
    null_earliest, null_latest = _statistics_date_range(
        null_group,
        latest_received,
    )
    if null_latest != latest_received:
        _fail_invariant("null-decision-date-range-mismatch")
    null_count = _integer(null_group, "record_count")
    mismatch_count = _integer(
        await _observe(
            "decision-date-mismatch-count",
            _count_url(_DECISION_DATE_MISMATCH_WHERE),
            session,
            evidence_store,
            inventory,
        ),
        "count",
    )
    issued_sample = _features(
        await _observe(
            "oldest-null-decision-sample",
            _sample_url("APPLICATION_DECISION IS NULL"),
            session,
            evidence_store,
            inventory,
        )
    )
    unresolved_profile = _first_attributes(
        await _observe(
            "unresolved-candidate-profile",
            _unresolved_profile_url(),
            session,
            evidence_store,
            inventory,
        )
    )
    unresolved_count = _integer(unresolved_profile, "record_count")
    unresolved_earliest, unresolved_latest = _statistics_date_range(
        unresolved_profile,
        latest_received,
    )
    if (
        unresolved_earliest >= _QUALIFICATION_START
        or unresolved_latest != latest_received
    ):
        _fail_invariant("unresolved-date-range-mismatch")
    unresolved_sample = _features(
        await _observe(
            "unresolved-oldest-sample",
            _sample_url(_UNRESOLVED_WHERE),
            session,
            evidence_store,
            inventory,
        )
    )
    issued_dates = _historical_sample_dates(
        issued_sample,
        latest_received,
        unresolved=False,
    )
    unresolved_dates = _historical_sample_dates(
        unresolved_sample,
        latest_received,
        unresolved=True,
    )
    contradictory = (
        null_count == _OBSERVED_NULL_DECISIONS
        and mismatch_count == 0
        and unresolved_count == _OBSERVED_UNRESOLVED_CANDIDATES
        and issued_dates[0] == null_earliest
        and unresolved_dates[0] == unresolved_earliest
        and any(item.get("Date_Issued") is not None for item in issued_sample)
        and any(
            isinstance(item.get("APPEAL_DECISION"), str)
            and bool(item["APPEAL_DECISION"].strip())
            for item in unresolved_sample
        )
        and any(
            item.get("TYPE") in {"Enforcement", "Master Plan"}
            for item in unresolved_sample
        )
    )
    if not contradictory:
        _fail_invariant("older-open-counterevidence-mismatch")
    return _OlderOpenFacts(
        application_decision_null_count=null_count,
        decision_date_mismatch_count=mismatch_count,
        unresolved_candidate_count=unresolved_count,
    )


async def _qualify(
    config: _Config,
    store: SqliteStore,
    session_factory: SessionFactory,
    now: Clock,
    *,
    verify_replay: bool = True,
) -> BirminghamBlockedQualificationReceiptV1:
    evidence_store = EvidenceStore(config.data_dir / "evidence")
    inventory: list[QueryObservationV1] = []
    session = session_factory()
    try:
        source = await _source_facts(session, evidence_store, inventory)

        recent = await _recent_facts(
            source.latest_received,
            session,
            evidence_store,
            inventory,
        )

        older_open = await _older_open_facts(
            source.latest_received,
            session,
            evidence_store,
            inventory,
        )
    finally:
        await session.aclose()

    registry = pilot_registry()
    readiness_before = registry.manifest(_AUTHORITY_ID).live_status.readiness
    sqlite_integrity = store.database_integrity()
    offline = _offline_result(config.data_dir, inventory, sqlite_integrity)
    readiness_after = registry.manifest(_AUTHORITY_ID).live_status.readiness
    if (
        readiness_before != LiveReadiness.BLOCKED
        or readiness_after != LiveReadiness.BLOCKED
    ):
        _fail_invariant("registry-readiness-changed")
    gap = SectionGapV1()
    receipt = BirminghamBlockedQualificationReceiptV1(
        created_at=now(),
        scope=config.scope,
        source_freshness=SourceFreshnessV1(
            latest_received=source.latest_received,
            latest_accepted=source.latest_accepted,
            calendar_2025_count=source.count_2025,
            july_2026_count=source.count_2026_july,
            august_2026_count=source.count_2026_august,
            rolling_30_day_count=recent.reported_count,
        ),
        recent_discovery=RecentDiscoveryV1(
            reported_count=recent.reported_count,
            page_feature_count=recent.feature_count,
            unique_reference_count=recent.unique_reference_count,
            unique_objectid_count=recent.unique_objectid_count,
            page_count=recent.page_count,
            latest_reference=recent.latest_reference,
            latest_received=recent.latest_received,
        ),
        older_open=OlderOpenV1(
            application_decision_null_count=(
                older_open.application_decision_null_count
            ),
            decision_date_mismatch_count=older_open.decision_date_mismatch_count,
            unresolved_candidate_count=older_open.unresolved_candidate_count,
            contradictions=(
                "issued-without-application-decision",
                "appeal-completed-without-decision-or-issued",
                "non-planning-register-record",
            ),
        ),
        active_appeals=ActiveAppealsV1(),
        sections=SectionGapsV1(
            documents=gap,
            comments=gap,
            conditions=gap,
            consultations=gap,
            relationships=gap,
        ),
        query_inventory=tuple(inventory),
        transport=TransportCostV1(
            request_count=len(inventory),
            attachment_body_requests=session.attachment_body_requests,
        ),
        local_integrity=LocalIntegrityV1(evidence_count=len(inventory)),
        offline_replay=offline,
        registry_readiness=RegistryReadinessV1(),
        checkpoint=BirminghamArcgisCheckpointV1(
            next_offset=recent.next_offset,
            reported_count=recent.reported_count,
            collected_count=recent.feature_count,
        ),
        weekly_cycles=(
            PendingWeeklyCycleV1(ordinal=1, required_offset_days=7),
            PendingWeeklyCycleV1(ordinal=2, required_offset_days=14),
        ),
    )
    if verify_replay:
        replay_session = _ReplaySession(config.data_dir, receipt.query_inventory)
        replayed = await _qualify(
            config,
            store,
            lambda: replay_session,
            lambda: receipt.created_at,
            verify_replay=False,
        )
        if not replay_session.complete or replayed != receipt:
            _fail_invariant("offline-replay-mismatch")
    return receipt


def replay_persisted_state(data_dir: Path) -> OfflineReplayV1:
    """Recompute the receipt from retained responses without network access."""
    receipt_path = data_dir / _RECEIPT_NAME
    receipt = BirminghamBlockedQualificationReceiptV1.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    database = data_dir / "yimby.sqlite3"
    if not database.is_file():
        _fail_invariant("missing-database")
    store = SqliteStore(database, EvidenceStore(data_dir / "evidence"))
    try:
        session = _ReplaySession(data_dir, receipt.query_inventory)
        replayed = asyncio.run(
            _qualify(
                _Config(data_dir=data_dir, scope=receipt.scope),
                store,
                lambda: session,
                lambda: receipt.created_at,
                verify_replay=False,
            )
        )
        if not session.complete or replayed != receipt:
            _fail_invariant("offline-replay-mismatch")
        return replayed.offline_replay
    finally:
        store.close()


def _write_receipt(
    path: Path,
    receipt: BirminghamBlockedQualificationReceiptV1,
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


def _default_session() -> HttpxPortalSession:
    return HttpxPortalSession()


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _error(code: str, exit_code: int) -> int:
    print(json.dumps({"error": code}, sort_keys=True), file=sys.stderr)
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory = _default_session,
    now: Clock = _default_clock,
) -> int:
    """Run one explicit ArcGIS assessment and publish only a blocked receipt."""
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except QualificationConfigError as error:
        return _error(str(error), 2)
    try:
        with ProcessLock(config.data_dir / "qualification.lock"):
            evidence = EvidenceStore(config.data_dir / "evidence")
            store = SqliteStore(config.data_dir / "yimby.sqlite3", evidence)
            try:
                registry = pilot_registry()
                store.register_authorities((registry.manifest(_AUTHORITY_ID),))
                receipt = asyncio.run(_qualify(config, store, session_factory, now))
            finally:
                store.close()
            _write_receipt(config.data_dir / _RECEIPT_NAME, receipt)
    except QualificationInvariantError as error:
        return _error(str(error), 1)
    except (OSError, RuntimeError, ValueError):
        return _error("runtime-failure", 1)
    print(receipt.model_dump_json())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
