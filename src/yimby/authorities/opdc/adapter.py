# Copyright (c) 2026 Kostas Stathoulopoulos

"""OPDC Agile Applications adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from html import unescape
from typing import TYPE_CHECKING, Annotated, NoReturn, Self
from urllib.parse import quote, urlencode

from pydantic import (
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from yimby.domain import (
    ApplicationMetadata,
    AuthorityCapabilities,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CapabilityState,
    CommentRecord,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    EvidenceCapture,
    FailedSection,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SectionState,
    SourceDefinition,
    SourceId,
    SourceReference,
    TransportMode,
    UnavailableSection,
    collection_state,
)
from yimby.geo import bng_to_wgs84
from yimby.transport import (
    PortalRequest,
    RequestHeader,
    RequestIntent,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("opdc-agile-applications")
BASE_URL = "https://planning.agileapplications.co.uk/opdc"
API_BASE_URL = "https://planningapi.agileapplications.co.uk"
_API_HEADERS = (
    RequestHeader(name="x-client", value="OPDC"),
    RequestHeader(name="x-product", value="CITIZENPORTAL"),
    RequestHeader(name="x-service", value="PA"),
)
_LIVE_PAGE = "live"
_NonBlank = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, strict=True),
]


class OpdcDiscoveryQuery(StrEnum):
    """One official Citizen Portal query in the OPDC bootstrap inventory."""

    REGISTERED_WINDOW = "registered-window"
    DETERMINED_WINDOW = "determined-window"
    REGISTERED_OPEN = "registered-open"


class OpdcDiscoveryScope(FrozenModel):
    """Exact inclusive live discovery scope owning resumable progress."""

    start: date
    end: date
    include_open: bool


class OpdcIdentity(FrozenModel):
    """Canonical public reference and Agile application identifier."""

    reference: _NonBlank
    locator: _NonBlank


class OpdcCompletedQuery(FrozenModel):
    """A fully returned API query with its exact returned identities."""

    query: OpdcDiscoveryQuery
    result_total: int = Field(ge=0)
    identities: tuple[OpdcIdentity, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Tie the declared total to a bijective per-query identity inventory."""
        if self.result_total != len(self.identities):
            _raise_checkpoint("result total")
        references = tuple(item.reference for item in self.identities)
        locators = tuple(item.locator for item in self.identities)
        if len(references) != len(set(references)) or len(locators) != len(
            set(locators)
        ):
            _raise_checkpoint("query identity")
        return self


class OpdcCheckpointV1(FrozenModel):
    """Fixture cursor plus coherent OPDC live-query progress."""

    page_token: str
    live_scope: OpdcDiscoveryScope | None = None
    completed_queries: tuple[OpdcCompletedQuery, ...] = ()
    seen_references: tuple[OpdcIdentity, ...] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Require live query progress to be an exact inventory prefix."""
        if self.live_scope is None:
            if (
                self.page_token == _LIVE_PAGE
                or self.completed_queries
                or self.seen_references
            ):
                _raise_checkpoint("fixture/live state")
            return self
        if self.page_token != _LIVE_PAGE:
            _raise_checkpoint("live page token")
        inventory = _query_inventory(self.live_scope)
        completed = tuple(item.query for item in self.completed_queries)
        if completed != inventory[: len(completed)]:
            _raise_checkpoint("query inventory")
        references = tuple(item.reference for item in self.seen_references)
        locators = tuple(item.locator for item in self.seen_references)
        if len(references) != len(set(references)) or len(locators) != len(
            set(locators)
        ):
            _raise_checkpoint("identity")
        if _merged_identities(self.completed_queries) != self.seen_references:
            _raise_checkpoint("identity inventory")
        return self

    @property
    def live_complete(self) -> bool:
        """Derive terminality from the exact completed-query inventory."""
        if self.live_scope is None:
            return False
        completed = tuple(item.query for item in self.completed_queries)
        return completed == _query_inventory(self.live_scope)


class _SearchRow(FrozenModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    id: int = Field(gt=0)
    reference: _NonBlank


class _SearchResponse(FrozenModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    total: int = Field(ge=0)
    results: tuple[_SearchRow, ...]


class OpdcDocumentV1(FrozenModel):
    """One OPDC document index row without attachment content."""

    document_id: _NonBlank
    title: _NonBlank
    url: HttpUrl
    received_date: date | None = None
    media_description: str | None = None
    source_name: str | None = None


class OpdcResponseV1(FrozenModel):
    """Public response text exposed directly by OPDC's portal API."""

    response_id: _NonBlank
    text: str
    received_date: date | None = None
    response_type: str | None = None


class OpdcApplicationV1(FrozenModel):
    """OPDC-native delegated planning application."""

    agile_case_id: str
    application_reference: str
    development_description: str
    workflow_status: str
    site_name: str
    documents: tuple[OpdcDocumentV1, ...]
    responses: tuple[OpdcResponseV1, ...]
    alternative_references: tuple[str, ...] = ()
    application_type: str | None = None
    decision: str | None = None
    received_date: date | None = None
    registration_date: date | None = None
    validated_date: date | None = None
    decision_date: date | None = None
    ward_name: str | None = None
    easting: float | None = None
    northing: float | None = None


class _DetailResponse(FrozenModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    id: int = Field(gt=0)
    reference: _NonBlank
    full_proposal: str | None = Field(default=None, alias="fullProposal")
    proposal: str | None = None
    status_owner: _NonBlank = Field(alias="statusOwner")
    location: _NonBlank
    one_application_reference: str = Field(default="", alias="oneAppReference")
    application_type: str | None = Field(default=None, alias="applicationType")
    decision_text: str | None = Field(default=None, alias="decisionText")
    received_date: str | None = Field(default=None, alias="receivedDate")
    registration_date: str | None = Field(default=None, alias="registrationDate")
    valid_date: str | None = Field(default=None, alias="validDate")
    decision_date: str | None = Field(default=None, alias="decisionDate")
    ward: str | None = None
    easting: int | float | None = None
    northing: int | float | None = None


class _DocumentResponse(FrozenModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    document_id: _NonBlank = Field(alias="documentId")
    description: str = ""
    name: str = ""
    media_description: str = Field(default="", alias="mediaDescription")
    received_date: str | None = Field(default=None, alias="receivedDate")


class _PublicResponse(FrozenModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    reply_id: int = Field(gt=0, alias="replyId")
    reply_text: str = Field(alias="replyLongText")
    reply_date: str | None = Field(default=None, alias="replyDate")
    reply_type: str | None = Field(default=None, alias="replyType")


_DOCUMENT_RESPONSES = TypeAdapter(tuple[_DocumentResponse, ...])
_PUBLIC_RESPONSES = TypeAdapter(tuple[_PublicResponse, ...])


class OpdcParseError(ValueError):
    """A required OPDC field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing OPDC field {field}")


class OpdcCheckpointError(ValueError):
    """A stored OPDC checkpoint contradicts its query or identity inventory."""

    def __init__(self, field: str) -> None:
        """Name the incoherent checkpoint field without exposing response data."""
        super().__init__(f"invalid OPDC checkpoint {field}")


class OpdcRoutingError(ValueError):
    """A live OPDC reference lacks its Agile application identifier."""

    def __init__(self, reference: str) -> None:
        """Name the public reference that cannot be routed."""
        super().__init__(f"OPDC reference has no locator: {reference}")


class OpdcIdentityMismatchError(ValueError):
    """The routed OPDC detail returned a different application identifier."""

    def __init__(self, expected: str, actual: str) -> None:
        """Describe the source identifier disagreement."""
        super().__init__(f"OPDC locator mismatch: expected {expected}, got {actual}")


class OpdcReferenceMismatchError(ValueError):
    """The routed OPDC detail returned a different public reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Describe the public reference disagreement."""
        super().__init__(f"OPDC reference mismatch: expected {expected}, got {actual}")


class OpdcAdapter:
    """Keep OPDC's Agile identifiers and inconclusive sections local."""

    manifest = AuthorityManifest(
        id=AuthorityId("opdc"),
        name="Old Oak and Park Royal Development Corporation",
        kind=AuthorityKind.DEVELOPMENT_CORPORATION,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(BASE_URL)),),
        capabilities=AuthorityCapabilities(
            discovery=CapabilityState.SUPPORTED,
            documents=CapabilityState.SUPPORTED,
            comments=CapabilityState.SUPPORTED,
            coordinates=CapabilityState.SUPPORTED,
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: OpdcCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[OpdcCheckpointV1]]:
        """Read fixture pages or the exact official Agile API inventory."""
        if session.mode != TransportMode.FIXTURE:
            async for batch in self._discover_live(session, window, checkpoint):
                yield batch
            return
        if checkpoint is not None and checkpoint.live_scope is not None:
            checkpoint = None
        page = "first" if checkpoint is None else checkpoint.page_token
        url = (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&page={quote(page)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-opdc-reference="([^"]+)"', html)
        )
        next_page = _required(html, r'data-opdc-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=OpdcCheckpointV1(page_token=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: OpdcCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[OpdcCheckpointV1]]:
        scope = OpdcDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != scope:
            progress = OpdcCheckpointV1(
                page_token=_LIVE_PAGE,
                live_scope=scope,
            )
        if progress.live_complete:
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=progress,
                complete=True,
            )
            return
        inventory = _query_inventory(scope)
        for query in inventory[len(progress.completed_queries) :]:
            capture = await session.fetch(_search_request(query, scope))
            identities = _parse_search(capture.body)
            progress, fresh = _advance(progress, query, identities)
            yield DiscoveryBatch(
                references=fresh,
                next_checkpoint=progress,
                complete=progress.live_complete,
            )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[OpdcApplicationV1]:
        """Read fixture detail or all implemented official API sections."""
        if session.mode != TransportMode.FIXTURE:
            return await self._fetch_live(session, reference)
        return await self._fetch_fixture(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[OpdcApplicationV1]:
        """Preserve the deterministic fixture contract."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = OpdcApplicationV1(
            agile_case_id=_required(
                html,
                r'data-opdc-case-id="([^"]+)"',
                "case id",
            ),
            application_reference=reference.reference,
            development_description=unescape(
                _required(html, r'data-opdc-proposal="([^"]+)"', "proposal")
            ),
            workflow_status=_required(
                html,
                r'data-opdc-status="([^"]+)"',
                "status",
            ),
            site_name=unescape(_required(html, r'data-opdc-site="([^"]+)"', "site")),
            documents=(),
            responses=(),
        )
        unavailable = UnavailableSection(reason="fixture does not expose this section")
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=unavailable,
                comments=unavailable,
            ),
            evidence=(detail,),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[OpdcApplicationV1]:
        """Read application, document metadata, and public response text."""
        if reference.locator is None:
            raise OpdcRoutingError(reference.reference)
        locator = reference.locator
        detail_capture = await session.fetch(
            _api_request(f"/api/application/{quote(locator, safe='')}")
        )
        detail = _parse_detail(detail_capture.body)
        if str(detail.id) != locator:
            raise OpdcIdentityMismatchError(locator, str(detail.id))
        if detail.reference != reference.reference:
            raise OpdcReferenceMismatchError(reference.reference, detail.reference)
        evidence = [detail_capture]
        documents, document_state = await _fetch_documents(
            session,
            locator,
            evidence,
        )
        responses, response_state = await _fetch_responses(
            session,
            locator,
            evidence,
        )
        proposal = _proposal(detail)
        alternative_reference = detail.one_application_reference.strip()
        alternatives = (
            (alternative_reference,)
            if alternative_reference and alternative_reference != detail.reference
            else ()
        )
        payload = OpdcApplicationV1(
            agile_case_id=locator,
            application_reference=detail.reference,
            development_description=proposal,
            workflow_status=detail.status_owner,
            site_name=detail.location,
            documents=documents,
            responses=responses,
            alternative_references=alternatives,
            application_type=_clean_optional(detail.application_type),
            decision=_clean_optional(detail.decision_text),
            received_date=_parse_api_date(detail.received_date, "received date"),
            registration_date=_parse_api_date(
                detail.registration_date,
                "registration date",
            ),
            validated_date=_parse_api_date(detail.valid_date, "valid date"),
            decision_date=_parse_api_date(detail.decision_date, "decision date"),
            ward_name=_clean_optional(detail.ward),
            easting=None if detail.easting is None else float(detail.easting),
            northing=None if detail.northing is None else float(detail.northing),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=document_state,
                comments=response_state,
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[OpdcApplicationV1],
    ) -> NormalisedObservation:
        """Map OPDC-native fields and complete public sections."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.development_description,
            status=payload.workflow_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=document.title, url=document.url)
                for document in payload.documents
            ),
            comments=tuple(
                CommentRecord(comment_id=response.response_id, text=response.text)
                for response in payload.responses
            ),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="opdc-v2",
            metadata=ApplicationMetadata(
                aliases=payload.alternative_references,
                application_type=payload.application_type,
                decision=payload.decision,
                address=payload.site_name,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                location=bng_to_wgs84(payload.easting, payload.northing),
            ),
        )


def _query_inventory(
    scope: OpdcDiscoveryScope,
) -> tuple[OpdcDiscoveryQuery, ...]:
    bounded = (
        OpdcDiscoveryQuery.REGISTERED_WINDOW,
        OpdcDiscoveryQuery.DETERMINED_WINDOW,
    )
    if not scope.include_open:
        return bounded
    return (*bounded, OpdcDiscoveryQuery.REGISTERED_OPEN)


def _merged_identities(
    completed_queries: tuple[OpdcCompletedQuery, ...],
) -> tuple[OpdcIdentity, ...]:
    by_reference: dict[str, str] = {}
    by_locator: dict[str, str] = {}
    merged = []
    for completed_query in completed_queries:
        for identity in completed_query.identities:
            known_locator = by_reference.get(identity.reference)
            known_reference = by_locator.get(identity.locator)
            if (known_locator is not None and known_locator != identity.locator) or (
                known_reference is not None and known_reference != identity.reference
            ):
                _raise_checkpoint("cross-query identity")
            if known_locator is None and known_reference is None:
                by_reference[identity.reference] = identity.locator
                by_locator[identity.locator] = identity.reference
                merged.append(identity)
    return tuple(merged)


def _search_request(
    query: OpdcDiscoveryQuery,
    scope: OpdcDiscoveryScope,
) -> PortalRequest:
    parameters: tuple[tuple[str, str], ...]
    if query == OpdcDiscoveryQuery.REGISTERED_WINDOW:
        parameters = (
            ("registrationDateFrom", scope.start.isoformat()),
            ("registrationDateTo", scope.end.isoformat()),
            ("status", "registered"),
        )
    elif query == OpdcDiscoveryQuery.DETERMINED_WINDOW:
        parameters = (
            ("decisionDateFrom", scope.start.isoformat()),
            ("decisionDateTo", scope.end.isoformat()),
            ("status", "determined"),
        )
    else:
        parameters = (("status", "registered"),)
    return _api_request(
        f"/api/application/search?{urlencode(parameters)}",
        RequestIntent.SEARCH,
    )


def _api_request(
    path: str,
    intent: RequestIntent = RequestIntent.DETAIL,
) -> PortalRequest:
    return PortalRequest(
        url=HttpUrl(f"{API_BASE_URL}{path}"),
        intent=intent,
        headers=_API_HEADERS,
    )


def _parse_detail(body: bytes) -> _DetailResponse:
    try:
        return _DetailResponse.model_validate_json(body)
    except ValidationError:
        return _raise_parse("detail JSON")


def _proposal(detail: _DetailResponse) -> str:
    proposal = _clean_optional(detail.full_proposal) or _clean_optional(detail.proposal)
    if proposal is None:
        return _raise_parse("detail proposal")
    return proposal


async def _fetch_documents(
    session: PortalSession,
    locator: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[OpdcDocumentV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            _api_request(f"/api/application/{quote(locator, safe='')}/document")
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        documents = _parse_documents(capture.body)
    except OpdcParseError as error:
        return (), FailedSection(code=error.code)
    return documents, collection_state(len(documents))


def _parse_documents(body: bytes) -> tuple[OpdcDocumentV1, ...]:
    try:
        rows = _DOCUMENT_RESPONSES.validate_json(body, strict=True)
    except ValidationError:
        return _raise_parse("documents JSON")
    identifiers = tuple(row.document_id for row in rows)
    if len(identifiers) != len(set(identifiers)):
        return _raise_parse("document identity")
    return tuple(
        OpdcDocumentV1(
            document_id=row.document_id,
            title=(
                _clean_optional(row.description)
                or _clean_optional(row.name)
                or _clean_optional(row.media_description)
                or "Document"
            ),
            url=HttpUrl(
                f"{API_BASE_URL}/api/application/document/OPDC/"
                f"{quote(row.document_id, safe='')}"
            ),
            received_date=_parse_api_date(
                row.received_date,
                "document received date",
            ),
            media_description=_clean_optional(row.media_description),
            source_name=_clean_optional(row.name),
        )
        for row in rows
    )


async def _fetch_responses(
    session: PortalSession,
    locator: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[OpdcResponseV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            _api_request(
                f"/api/application/{quote(locator, safe='')}/responses",
                RequestIntent.COMMENTS,
            )
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        responses = _parse_responses(capture.body)
    except OpdcParseError as error:
        return (), FailedSection(code=error.code)
    return responses, collection_state(len(responses))


def _parse_responses(body: bytes) -> tuple[OpdcResponseV1, ...]:
    try:
        rows = _PUBLIC_RESPONSES.validate_json(body, strict=True)
    except ValidationError:
        return _raise_parse("responses JSON")
    identifiers = tuple(row.reply_id for row in rows)
    if len(identifiers) != len(set(identifiers)):
        return _raise_parse("response identity")
    return tuple(
        OpdcResponseV1(
            response_id=str(row.reply_id),
            text=row.reply_text,
            received_date=_parse_api_date(row.reply_date, "response date"),
            response_type=_clean_optional(row.reply_type),
        )
        for row in rows
    )


def _parse_api_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return _raise_parse(field)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_search(body: bytes) -> tuple[OpdcIdentity, ...]:
    try:
        response = _SearchResponse.model_validate_json(body)
    except ValidationError:
        _raise_parse("search JSON")
    if response.total != len(response.results):
        _raise_parse(
            f"search total expected {response.total} actual {len(response.results)}"
        )
    identities = tuple(
        OpdcIdentity(reference=row.reference, locator=str(row.id))
        for row in response.results
    )
    references = tuple(item.reference for item in identities)
    locators = tuple(item.locator for item in identities)
    if len(references) != len(set(references)) or len(locators) != len(set(locators)):
        _raise_parse("search identity")
    return identities


def _advance(
    progress: OpdcCheckpointV1,
    query: OpdcDiscoveryQuery,
    identities: tuple[OpdcIdentity, ...],
) -> tuple[OpdcCheckpointV1, tuple[SourceReference, ...]]:
    by_reference = {
        identity.reference: identity.locator for identity in progress.seen_references
    }
    by_locator = {
        identity.locator: identity.reference for identity in progress.seen_references
    }
    seen = list(progress.seen_references)
    fresh = []
    for identity in identities:
        known_locator = by_reference.get(identity.reference)
        known_reference = by_locator.get(identity.locator)
        if (known_locator is not None and known_locator != identity.locator) or (
            known_reference is not None and known_reference != identity.reference
        ):
            _raise_parse("cross-query identity")
        if known_locator is None and known_reference is None:
            by_reference[identity.reference] = identity.locator
            by_locator[identity.locator] = identity.reference
            seen.append(identity)
            fresh.append(
                SourceReference(
                    source_id=SOURCE,
                    reference=identity.reference,
                    locator=identity.locator,
                )
            )
    checkpoint = OpdcCheckpointV1(
        page_token=_LIVE_PAGE,
        live_scope=progress.live_scope,
        completed_queries=(
            *progress.completed_queries,
            OpdcCompletedQuery(
                query=query,
                result_total=len(identities),
                identities=identities,
            ),
        ),
        seen_references=tuple(seen),
    )
    return checkpoint, tuple(fresh)


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise OpdcParseError(field)
    return match.group(1).strip()


def _raise_parse(field: str) -> NoReturn:
    raise OpdcParseError(field)


def _raise_checkpoint(field: str) -> NoReturn:
    raise OpdcCheckpointError(field)
