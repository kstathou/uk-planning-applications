# Copyright (c) 2026 Kostas Stathoulopoulos

"""Typed planning collection domain."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

AuthorityId = NewType("AuthorityId", str)
SourceId = NewType("SourceId", str)
ApplicationId = NewType("ApplicationId", str)
EvidenceDigest = NewType("EvidenceDigest", str)


class FrozenModel(BaseModel):
    """Immutable base for values passed between collection boundaries."""

    model_config = ConfigDict(frozen=True)


class AuthorityKind(StrEnum):
    """Planning authority classification."""

    COUNTY = "county"
    DEVELOPMENT_CORPORATION = "development-corporation"
    DISTRICT = "district"
    LONDON_BOROUGH = "london-borough"
    METROPOLITAN = "metropolitan"
    NATIONAL_PARK = "national-park"
    UNITARY = "unitary"


class CapabilityState(StrEnum):
    """Whether one public portal capability is known to be exposed."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class TransportMode(StrEnum):
    """Transport most recently used for an authority."""

    FIXTURE = "fixture"
    BROWSER = "browser"
    LIVE = "live"
    NOT_RUN = "not-run"


class LiveReadiness(StrEnum):
    """Truthful authority status at the live collection boundary."""

    BLOCKED = "blocked"
    BROWSER_ONLY = "browser-only"
    DISCOVERY_ONLY = "discovery-only"
    LIVE_READY = "live-ready"


class LiveTransportKind(StrEnum):
    """Transport required by a live-ready authority package."""

    BROWSER = "browser"
    HTTP = "http"


class LiveStatus(FrozenModel):
    """Auditable live readiness, separate from fixture implementation."""

    readiness: LiveReadiness
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    transport: LiveTransportKind | None = None


class RunStatus(StrEnum):
    """Durable lifecycle state for one collection run."""

    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"


class AuthorityCapabilities(FrozenModel):
    """Known portal capabilities without assuming missing research is absence."""

    discovery: CapabilityState = CapabilityState.SUPPORTED
    documents: CapabilityState = CapabilityState.UNKNOWN
    comments: CapabilityState = CapabilityState.UNKNOWN
    coordinates: CapabilityState = CapabilityState.UNKNOWN


class SourceDefinition(FrozenModel):
    """One portal used by an authority for a bounded period."""

    id: SourceId
    base_url: HttpUrl
    valid_from: date | None = None
    valid_to: date | None = None


class AuthorityManifest(FrozenModel):
    """Stable authority identity and its dated source inventory."""

    id: AuthorityId
    name: str
    kind: AuthorityKind
    sources: tuple[SourceDefinition, ...] = Field(min_length=1)
    capabilities: AuthorityCapabilities = AuthorityCapabilities()
    live_status: LiveStatus = LiveStatus(
        readiness=LiveReadiness.BLOCKED,
        reason="live collection has not been assessed",
        evidence=("no live evidence recorded",),
    )


class CompleteSection(FrozenModel):
    """A section whose exposed items were fully enumerated."""

    kind: Literal["complete"] = "complete"
    item_count: int = Field(ge=0)


class EmptySection(FrozenModel):
    """A section confirmed to contain no exposed items."""

    kind: Literal["empty"] = "empty"


class UnavailableSection(FrozenModel):
    """A section the portal does not expose."""

    kind: Literal["unavailable"] = "unavailable"
    reason: str


class ExcludedSection(FrozenModel):
    """A section intentionally omitted by collection policy."""

    kind: Literal["excluded"] = "excluded"
    policy: str


class FailedSection(FrozenModel):
    """A section whose retrieval failed during this observation."""

    kind: Literal["failed"] = "failed"
    code: str


SectionState = Annotated[
    CompleteSection
    | EmptySection
    | UnavailableSection
    | ExcludedSection
    | FailedSection,
    Field(discriminator="kind"),
]


def collection_state(count: int) -> CompleteSection | EmptySection:
    """Describe a successfully enumerated collection section."""
    if count == 0:
        return EmptySection()
    return CompleteSection(item_count=count)


class Completeness(FrozenModel):
    """Explicit state for every section implemented by the first slice."""

    application: SectionState
    documents: SectionState
    comments: SectionState


class DiscoveryWindow(FrozenModel):
    """Inclusive dates and open-case policy for discovery."""

    start: date
    end: date
    include_open: bool = True


class SourceReference(FrozenModel):
    """An authority-native application identifier."""

    source_id: SourceId
    reference: str
    locator: str | None = None


class StoredCheckpoint(FrozenModel):
    """Type-erased checkpoint persisted by the common runner."""

    schema_version: int = Field(ge=1)
    payload_json: str


class DiscoveryBatch[CheckpointT: BaseModel](FrozenModel):
    """References and the checkpoint that becomes valid with them."""

    references: tuple[SourceReference, ...]
    next_checkpoint: CheckpointT
    complete: bool


class DurableDiscoveryBatch(FrozenModel):
    """Type-erased discovery result accepted by persistence."""

    references: tuple[SourceReference, ...]
    next_checkpoint: StoredCheckpoint
    complete: bool


class EvidenceCapture(FrozenModel):
    """One permitted response retained as source evidence."""

    url: HttpUrl
    media_type: str
    body: bytes
    digest: EvidenceDigest


class NativeDocument(FrozenModel):
    """Document metadata without attachment content."""

    title: str
    url: HttpUrl


class NativeComment(FrozenModel):
    """Comment text exposed directly by a portal."""

    comment_id: str
    text: str


class NativeSnapshot[NativeT: BaseModel](FrozenModel):
    """Typed authority payload and the evidence used to derive it."""

    reference: SourceReference
    observed_at: datetime
    payload: NativeT
    completeness: Completeness
    evidence: tuple[EvidenceCapture, ...]


class Provenance(FrozenModel):
    """Origin of one normalised field."""

    field: str
    evidence: EvidenceDigest


class DocumentRecord(FrozenModel):
    """Normalised document metadata."""

    title: str
    url: HttpUrl


class CommentRecord(FrozenModel):
    """Normalised exposed comment text."""

    comment_id: str
    text: str


class Wgs84Coordinate(FrozenModel):
    """Longitude and latitude in the public WGS84 coordinate system."""

    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class ApplicationLocation(FrozenModel):
    """Source BNG coordinates and their WGS84 projection."""

    bng_easting: float
    bng_northing: float
    wgs84: Wgs84Coordinate


class ApplicationEvent(FrozenModel):
    """One authority-published event in an application's timeline."""

    event_type: str
    event_at: datetime
    details: str | None = None


class ApplicationRelationship(FrozenModel):
    """A typed link to another public planning reference."""

    related_reference: str
    relationship_type: str


class ApplicationMetadata(FrozenModel):
    """Common optional fields that remain separate from required core sections."""

    aliases: tuple[str, ...] = ()
    application_type: str | None = None
    decision: str | None = None
    address: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision_date: date | None = None
    location: ApplicationLocation | None = None
    source_url: HttpUrl | None = None
    published_parties: tuple[str, ...] = ()
    officer_name: str | None = None
    constraints: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    consultations: tuple[str, ...] = ()
    events: tuple[ApplicationEvent, ...] = ()
    relationships: tuple[ApplicationRelationship, ...] = ()


class NormalisedObservation(FrozenModel):
    """Common application sections ready for semantic persistence."""

    authority_id: AuthorityId
    reference: SourceReference
    proposal: str
    status: str
    documents: tuple[DocumentRecord, ...]
    comments: tuple[CommentRecord, ...]
    completeness: Completeness
    provenance: tuple[Provenance, ...]
    normaliser_version: str
    metadata: ApplicationMetadata = ApplicationMetadata()


class CollectedObservation(FrozenModel):
    """Native and normalised forms committed as one observation."""

    native_schema: str
    native_json: str
    normalised: NormalisedObservation
    evidence: tuple[EvidenceCapture, ...]
    observed_at: datetime


class StoredApplication(FrozenModel):
    """Current successful sections returned to consumers."""

    id: ApplicationId
    authority_id: AuthorityId
    reference: str
    proposal: str
    status: str
    documents: tuple[DocumentRecord, ...]
    comments: tuple[CommentRecord, ...]
    completeness: Completeness


class DiscoveryState(FrozenModel):
    """Durable references and their matching checkpoint."""

    references: tuple[str, ...]
    queued: tuple[SourceReference, ...]
    checkpoint: StoredCheckpoint | None


class QualificationSnapshot(FrozenModel):
    """Authority-scoped durable counts used by live qualification."""

    authority_id: AuthorityId
    applications: int = Field(ge=0)
    discovered_references: int = Field(ge=0)
    native_versions: int = Field(ge=0)
    application_versions: int = Field(ge=0)
    document_versions: int = Field(ge=0)
    comment_versions: int = Field(ge=0)
    pending_retries: int = Field(ge=0)
    failed_sections: int = Field(ge=0)
    unmapped_records: int = Field(ge=0)


class CollectionReport(FrozenModel):
    """Observable result of one authority collection."""

    applications: tuple[ApplicationId, ...]
    requested_urls: tuple[str, ...]
    attachment_body_requests: int


class AuthorityCollectionStatus(StrEnum):
    """Per-authority result emitted by failure-isolated orchestration."""

    FAILED = "failed"
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"


class AuthorityCollectionResult(FrozenModel):
    """Structured outcome for one authority in a coordinated run."""

    authority_id: AuthorityId
    status: AuthorityCollectionStatus
    applications: tuple[ApplicationId, ...] = ()
    attachment_body_requests: int = Field(default=0, ge=0)
    failure_code: str | None = None
    message: str | None = None


class RetainedNativeRecord(FrozenModel):
    """Latest retained native input sufficient for offline normalisation."""

    application_id: ApplicationId
    authority_id: AuthorityId
    reference: SourceReference
    native_schema: str
    native_json: str
    observed_at: datetime
    completeness: Completeness
    evidence: tuple[EvidenceCapture, ...]


class RunMetrics(FrozenModel):
    """Collection costs persisted for operations and dashboards."""

    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    browser_time_ms: int = Field(default=0, ge=0)
    attachment_body_requests: int = Field(default=0, ge=0)
    storage_growth_bytes: int = Field(ge=0)


class RunCostSnapshot(FrozenModel):
    """Durable source cost and state for one collection run."""

    status: RunStatus
    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    browser_time_ms: int = Field(ge=0)
    attachment_body_requests: int = Field(ge=0)


class RunOutcome(FrozenModel):
    """Final state and measurements committed together for one run."""

    status: RunStatus
    metrics: RunMetrics
    transport_mode: TransportMode
    failure_message: str | None = None


class RetryItem(FrozenModel):
    """A detail reference that remains retryable after a failed run."""

    authority_id: AuthorityId
    reference: SourceReference
    attempts: int = Field(ge=1)
    last_error: str
    status: Literal["pending", "succeeded"]


class AuthorityOperationalState(FrozenModel):
    """Registry ownership, collection freshness, failures, and backlog."""

    manifest: AuthorityManifest
    implementation_status: Literal["fixture-ready"]
    transport_mode: TransportMode
    last_success_at: datetime | None
    freshness_days: int | None = Field(default=None, ge=0)
    failure_count: int = Field(ge=0)
    backlog_count: int = Field(ge=0)


class ApplicationView(FrozenModel):
    """Inspection model combining current semantic sections and rich metadata."""

    application: StoredApplication
    metadata: ApplicationMetadata
    normaliser_version: str
    observed_at: datetime
    suppressed: bool


class NormalisationReport(FrozenModel):
    """Outcome of rebuilding current semantics from retained native payloads."""

    rebuilt: int = Field(ge=0)
    transport_requests: int = Field(ge=0)


class DoctorCheck(FrozenModel):
    """One actionable local health diagnostic."""

    name: str
    ok: bool
    detail: str


class DoctorReport(FrozenModel):
    """Local database, evidence, migration, and disk-space health."""

    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        """Return true only when every diagnostic passes."""
        return all(check.ok for check in self.checks)


class DashboardAuthority(FrozenModel):
    """Per-authority dashboard row."""

    authority_id: AuthorityId
    name: str
    implementation_status: str
    live_readiness: LiveReadiness
    live_reason: str
    live_evidence: tuple[str, ...]
    transport_mode: TransportMode
    freshness_days: int | None
    failures: int = Field(ge=0)
    backlog: int = Field(ge=0)


class DashboardSnapshot(FrozenModel):
    """GUI-independent operational dashboard data."""

    coverage_implemented: int = Field(ge=0)
    coverage_denominator: int = Field(ge=1)
    live_ready: int = Field(ge=0)
    live_readiness_denominator: int = Field(ge=1)
    authorities: tuple[DashboardAuthority, ...]
    request_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    browser_time_ms: int = Field(ge=0)
    storage_growth_bytes: int = Field(ge=0)
    application_count: int = Field(ge=0)
    observed_change_count: int = Field(ge=0)
    unmapped_count: int = Field(ge=0)


class ApplicationSearchHit(FrozenModel):
    """Small application search result used by the local dashboard."""

    application_id: ApplicationId
    authority_id: AuthorityId
    reference: str
    proposal: str
    address: str | None
    location: ApplicationLocation | None
