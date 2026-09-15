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
    checkpoint: StoredCheckpoint | None


class CollectionReport(FrozenModel):
    """Observable result of one authority collection."""

    applications: tuple[ApplicationId, ...]
    requested_urls: tuple[str, ...]
    attachment_body_requests: int
