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
    ValidationError,
    model_validator,
)

from yimby.domain import (
    AuthorityCapabilities,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CapabilityState,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    TransportMode,
    UnavailableSection,
)
from yimby.transport import PortalRequest, RequestHeader, RequestIntent

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


class OpdcCompletedQuery(FrozenModel):
    """A fully returned API query and its source-declared result total."""

    query: OpdcDiscoveryQuery
    result_total: int = Field(ge=0)


class OpdcIdentity(FrozenModel):
    """Canonical public reference and Agile application identifier."""

    reference: _NonBlank
    locator: _NonBlank


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


class OpdcApplicationV1(FrozenModel):
    """OPDC-native delegated planning application."""

    agile_case_id: str
    development_description: str
    workflow_status: str
    site_name: str


class OpdcParseError(ValueError):
    """A required OPDC field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing OPDC field {field}")


class OpdcCheckpointError(ValueError):
    """A stored OPDC checkpoint contradicts its query or identity inventory."""

    def __init__(self, field: str) -> None:
        """Name the incoherent checkpoint field without exposing response data."""
        super().__init__(f"invalid OPDC checkpoint {field}")


class OpdcAdapter:
    """Keep OPDC's Agile identifiers and inconclusive sections local."""

    manifest = AuthorityManifest(
        id=AuthorityId("opdc"),
        name="Old Oak and Park Royal Development Corporation",
        kind=AuthorityKind.DEVELOPMENT_CORPORATION,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(BASE_URL)),),
        capabilities=AuthorityCapabilities(discovery=CapabilityState.UNKNOWN),
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
        """Read a fixture record without claiming live Agile agreement."""
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
            development_description=unescape(
                _required(html, r'data-opdc-proposal="([^"]+)"', "proposal")
            ),
            workflow_status=_required(
                html,
                r'data-opdc-status="([^"]+)"',
                "status",
            ),
            site_name=unescape(_required(html, r'data-opdc-site="([^"]+)"', "site")),
        )
        unavailable = UnavailableSection(reason="live client bootstrap inconclusive")
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

    def normalise(
        self,
        snapshot: NativeSnapshot[OpdcApplicationV1],
    ) -> NormalisedObservation:
        """Map OPDC fixture fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.development_description,
            status=snapshot.payload.workflow_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="opdc-v1",
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


def _search_request(
    query: OpdcDiscoveryQuery,
    scope: OpdcDiscoveryScope,
) -> PortalRequest:
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
    return PortalRequest(
        url=HttpUrl(f"{API_BASE_URL}/api/application/search?{urlencode(parameters)}"),
        intent=RequestIntent.SEARCH,
        headers=_API_HEADERS,
    )


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
            OpdcCompletedQuery(query=query, result_total=len(identities)),
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
