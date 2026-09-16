# Copyright (c) 2026 Kostas Stathoulopoulos

"""Devon-owned fixture and live county planning-register adapter."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn, Self
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import Field, HttpUrl, model_validator

from yimby.domain import (
    ApplicationEvent,
    ApplicationMetadata,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
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
from yimby.geo import bng_to_wgs84
from yimby.transport import (
    FormField,
    PortalRequest,
    RedirectBoundary,
    RequestIntent,
    RequestMethod,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalSession

SOURCE = SourceId("devon-custom-register")
BASE_URL = "https://planning.devon.gov.uk"
_ADVANCED_FORM_URL = f"{BASE_URL}/Search/Advanced"
_RESULTS_URL = f"{BASE_URL}/Search/Results"
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")
_MAX_WINDOW_DAYS = 30
_PAGE_SIZE = 10
_FIRST_PAGED_RESULT = 2
_DOCUMENT_COLUMN_COUNT = 3
_REDIRECT_BOUNDARY = RedirectBoundary(
    origin=HttpUrl(f"{BASE_URL}/"),
    exact_paths=(
        "/Disclaimer",
        "/Disclaimer/Accept",
        "/Search/Advanced",
        "/Search/Results",
    ),
    path_prefixes=("/Planning/Display/", "/Search/Results/"),
)
_BOOLEAN_FIELDS = (
    "Outstanding",
    "SearchPlanning",
    "SearchEnforcement",
    "SearchAppeals",
)
_VALUE_FIELDS = (
    "ApplicationOrDistrictNumbers",
    "Address",
    "Proposal",
    "Parish",
    "Ward",
    "District",
    "Radius",
    "Decision",
    "DateReceivedFrom",
    "DateReceivedTo",
    "DateDeterminedFrom",
    "DateDeterminedTo",
    "ApplicationType",
    "AppealMethod",
    "AppealDecision",
    "PinsRef",
    "DateAppealFrom",
    "DateAppealTo",
    "DateAppealDecisionFrom",
    "DateAppealDecisionTo",
)


class DevonDiscoveryScope(FrozenModel):
    """Exact live discovery request owning resumable Devon progress."""

    start: date
    end: date
    include_open: bool


class DevonPageLinkV1(FrozenModel):
    """One numbered locator exposed by Devon's result pager."""

    page: int = Field(ge=1)
    locator: HttpUrl


class DevonPageProofV1(FrozenModel):
    """Observable proof for one committed nonterminal result page."""

    page: int = Field(ge=1)
    references: tuple[SourceReference, ...] = Field(min_length=1)
    numbered_pages: tuple[int, ...] = Field(min_length=1)
    numbered_links: tuple[DevonPageLinkV1, ...]
    next_locator: HttpUrl


class DevonQuerySummaryV1(FrozenModel):
    """Durable row and page totals for one completed discovery query."""

    query_key: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    page_count: int = Field(ge=1)


class DevonCheckpointV1(FrozenModel):
    """Fixture cursor plus validated resumable Devon discovery progress."""

    result_page: str
    live_scope: DevonDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    query_summaries: tuple[DevonQuerySummaryV1, ...] = ()
    active_query: str | None = None
    next_page: int = Field(default=1, ge=1)
    active_pages: tuple[DevonPageProofV1, ...] = ()
    seen_references: tuple[SourceReference, ...] = ()
    live_complete: bool = False

    @model_validator(mode="after")
    def validate_live_progress(self) -> Self:  # noqa: C901, PLR0912
        """Exclude contradictory fixture, active, and terminal states."""
        if self.live_scope is None:
            if (
                self.completed_queries
                or self.query_summaries
                or self.active_query is not None
                or self.next_page != 1
                or self.active_pages
                or self.seen_references
                or self.live_complete
            ):
                _raise_checkpoint("live-scope-required")
            return self
        if self.result_page != "live":
            _raise_checkpoint("live-result-cursor-required")
        keys = _query_keys(self.live_scope)
        if self.completed_queries != keys[: len(self.completed_queries)]:
            _raise_checkpoint("completed-query-prefix")
        if tuple(item.query_key for item in self.query_summaries) != (
            self.completed_queries
        ):
            _raise_checkpoint("completed-query-summaries")
        if len({item.reference for item in self.seen_references}) != len(
            self.seen_references
        ) or any(item.locator is None for item in self.seen_references):
            _raise_checkpoint("seen-references")
        if self.live_complete:
            if (
                self.completed_queries != keys
                or self.active_query is not None
                or self.next_page != 1
                or self.active_pages
            ):
                _raise_checkpoint("terminal-incoherent")
            return self
        if self.active_query is None:
            if self.next_page != 1 or self.active_pages:
                _raise_checkpoint("inactive-page-progress")
            return self
        if (
            len(self.completed_queries) >= len(keys)
            or self.active_query != keys[len(self.completed_queries)]
            or self.next_page != len(self.active_pages) + 1
            or not self.active_pages
        ):
            _raise_checkpoint("active-query-incoherent")
        for expected_page, proof in enumerate(self.active_pages, start=1):
            if proof.page != expected_page or len(proof.references) != _PAGE_SIZE:
                _raise_checkpoint("active-page-incoherent")
        return self


class DevonQualificationAuditV1(FrozenModel):
    """Adapter-owned projection of qualification-relevant checkpoint facts."""

    scope: DevonDiscoveryScope
    expected_queries: tuple[str, ...]
    completed_queries: tuple[str, ...]
    query_summaries: tuple[DevonQuerySummaryV1, ...]
    terminal_coherent: bool
    references: tuple[SourceReference, ...]


class DevonDocumentV1(FrozenModel):
    """Devon document metadata retained without the attachment body."""

    title: str
    url: HttpUrl
    module: str | None = None
    record_number: str | None = None
    plan_identifier: str | None = None
    image_identifier: str | None = None
    is_plan: bool | None = None
    filename: str | None = None
    category: str | None = None
    published_date: date | None = None


class DevonConsultationV1(FrozenModel):
    """One source row retained without guessing around malformed cells."""

    values: tuple[str, ...] = Field(min_length=1)


class DevonApplicationV1(FrozenModel):
    """Devon-native minerals, waste, or county development record."""

    council_reference: str
    application_type: str
    proposal_description: str
    public_status: str
    site_location: str
    documents: tuple[DevonDocumentV1, ...]
    case_officer: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision: str | None = None
    decision_date: date | None = None
    district: str | None = None
    electoral_division: str | None = None
    parish: str | None = None
    applicant: str | None = None
    agent: str | None = None
    consultation_expiry_date: date | None = None
    decision_level: str | None = None
    committee_date: date | None = None
    issue_date: date | None = None
    applicant_address: str | None = None
    agent_address: str | None = None
    local_members: tuple[str, ...] = ()
    bng_easting: float | None = None
    bng_northing: float | None = None
    constraints: tuple[str, ...] = ()
    constraints_exposed: bool = False
    consultations: tuple[DevonConsultationV1, ...] = ()
    consultations_exposed: bool = False


class _DevonQuery(FrozenModel):
    kind: Literal[
        "received",
        "determined",
        "outstanding-planning",
        "appeal-received",
        "appeal-determined",
        "outstanding-appeals",
    ]
    key: str
    start: date | None = None
    end: date | None = None


class _DiscoveryPage(FrozenModel):
    references: tuple[SourceReference, ...]
    page: int = Field(ge=1)
    numbered_pages: tuple[int, ...]
    numbered_links: tuple[DevonPageLinkV1, ...]
    next_locator: HttpUrl | None
    terminal: bool

    def committed_proof(self) -> DevonPageProofV1:
        """Convert a nonterminal response into its durable replay proof."""
        if self.terminal or self.next_locator is None:
            _raise_pagination("terminal-continuation")
        return DevonPageProofV1(
            page=self.page,
            references=self.references,
            numbered_pages=self.numbered_pages,
            numbered_links=self.numbered_links,
            next_locator=self.next_locator,
        )


class DevonAdapter:
    """Own Devon disclaimer, advanced-search, pager, and detail semantics."""

    manifest = AuthorityManifest(
        id=AuthorityId("devon"),
        name="Devon County Council",
        kind=AuthorityKind.COUNTY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DevonCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DevonCheckpointV1]]:
        """Use fixtures or Devon's bounded received, determined, and open searches."""
        if session.mode == TransportMode.FIXTURE:
            async for batch in self._discover_fixture(session, window, checkpoint):
                yield batch
            return
        async for batch in self._discover_live(session, window, checkpoint):
            yield batch

    async def _discover_fixture(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DevonCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DevonCheckpointV1]]:
        page = "1" if checkpoint is None else checkpoint.result_page
        url = (
            f"{BASE_URL}/Search/Results?receivedFrom={window.start.isoformat()}"
            f"&receivedTo={window.end.isoformat()}&page={quote(page)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-devon-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(html, r'data-devon-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=DevonCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DevonCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DevonCheckpointV1]]:
        _validate_window(window)
        scope = DevonDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint or DevonCheckpointV1(result_page="live", live_scope=scope)
        if progress.live_scope != scope:
            if not progress.live_complete:
                _raise_checkpoint("scope-mismatch")
            progress = DevonCheckpointV1(result_page="live", live_scope=scope)
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        queries = _query_inventory(scope)
        for query in queries[len(progress.completed_queries) :]:
            form_capture = await _fetch_protected(
                session,
                PortalRequest(
                    url=HttpUrl(_ADVANCED_FORM_URL), intent=RequestIntent.SEARCH
                ),
            )
            form = _parse_advanced_form(form_capture[-1].body)
            first_capture = await _fetch_protected(
                session,
                PortalRequest(
                    url=HttpUrl(_RESULTS_URL),
                    intent=RequestIntent.SEARCH,
                    method=RequestMethod.POST,
                    form=_advanced_fields(form, query),
                ),
            )
            current = _parse_discovery_page(first_capture[-1].body, expected_page=1)
            if progress.active_query == query.key:
                current = await _replay_committed_pages(session, progress, current)
            while True:
                progress, fresh = _advance_checkpoint(
                    progress,
                    query=query,
                    page=current,
                    all_query_keys=tuple(item.key for item in queries),
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=progress,
                    complete=progress.live_complete,
                )
                if current.terminal:
                    break
                current = await _fetch_result_page(
                    session,
                    current.committed_proof().next_locator,
                    progress.next_page,
                )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DevonApplicationV1]:
        """Read fixture detail or Devon's complete hidden-tab document."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DevonApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{BASE_URL}/Application/Detail?ref={encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        documents = tuple(
            DevonDocumentV1(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-devon-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = DevonApplicationV1(
            council_reference=reference.reference,
            application_type=_required_fixture(
                html, r'data-devon-type="([^"]+)"', "application type"
            ),
            proposal_description=unescape(
                _required_fixture(html, r'data-devon-proposal="([^"]+)"', "proposal")
            ),
            public_status=_required_fixture(
                html, r'data-devon-status="([^"]+)"', "status"
            ),
            site_location=unescape(
                _required_fixture(html, r'data-devon-location="([^"]+)"', "location")
            ),
            documents=documents,
        )
        return _snapshot(
            reference,
            payload,
            (detail,),
            CompleteSection(item_count=len(documents)),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DevonApplicationV1]:
        if reference.source_id != SOURCE or reference.locator is None:
            raise DevonRoutingError(reference.reference)
        captures = await _fetch_protected(
            session,
            PortalRequest(url=HttpUrl(reference.locator), intent=RequestIntent.DETAIL),
        )
        detail = captures[-1]
        fields = _parse_labelled_fields(detail.body)
        published = _required_field(
            fields, "application number", "reference", "application reference"
        )
        if published != reference.reference:
            raise DevonReferenceMismatchError(reference.reference, published)
        documents, document_state = _parse_documents(detail.body)
        bng_easting, bng_northing = _parse_coordinates(detail.body)
        constraints, constraints_exposed = _parse_constraints(detail.body)
        consultations, consultations_exposed = _parse_consultations(detail.body)
        payload = DevonApplicationV1(
            council_reference=published,
            application_type=_required_field(fields, "application type", "type"),
            proposal_description=_required_field(fields, "proposal", "description"),
            public_status=_required_field(fields, "status"),
            site_location=_required_field(fields, "location", "site location"),
            documents=documents,
            case_officer=_optional_field(fields, "case officer"),
            received_date=_optional_date(fields, "date received", "received date"),
            validated_date=_optional_date(
                fields, "date valid", "validation date", "validated date"
            ),
            decision=_optional_field(fields, "decision"),
            decision_date=_optional_date(fields, "decision date"),
            district=_optional_field(fields, "district(s)", "district"),
            electoral_division=_optional_field(
                fields, "electoral division(s)", "electoral division"
            ),
            parish=_optional_field(fields, "parish(es)", "parish"),
            applicant=_optional_field(fields, "applicant"),
            agent=_optional_field(fields, "agent"),
            consultation_expiry_date=_optional_date(fields, "consultation expiry"),
            decision_level=_optional_field(fields, "decision level"),
            committee_date=_optional_date(fields, "committee date"),
            issue_date=_optional_date(fields, "issue date"),
            applicant_address=_optional_field(fields, "applicant's address"),
            agent_address=_optional_field(fields, "agent's address"),
            local_members=_split_lines(_optional_field(fields, "local member(s)")),
            bng_easting=bng_easting,
            bng_northing=bng_northing,
            constraints=constraints,
            constraints_exposed=constraints_exposed,
            consultations=consultations,
            consultations_exposed=consultations_exposed,
        )
        return _snapshot(reference, payload, captures, document_state)

    def normalise(
        self, snapshot: NativeSnapshot[DevonApplicationV1]
    ) -> NormalisedObservation:
        """Map Devon-native fields to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[-1].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_description,
            status=payload.public_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(
                    title=item.title,
                    url=item.url,
                    category=item.category,
                    published_date=item.published_date,
                )
                for item in payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="devon-v4",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                decision=payload.decision,
                address=payload.site_location,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                location=bng_to_wgs84(payload.bng_easting, payload.bng_northing),
                published_parties=tuple(
                    value for value in (payload.applicant, payload.agent) if value
                ),
                officer_name=payload.case_officer,
                constraints=payload.constraints,
                consultations=tuple(
                    " | ".join(item.values) for item in payload.consultations
                ),
                events=tuple(
                    ApplicationEvent(
                        event_type=event_type,
                        event_at=datetime.combine(event_date, datetime.min.time(), UTC),
                    )
                    for event_type, event_date in (
                        ("consultation-expiry", payload.consultation_expiry_date),
                        ("committee", payload.committee_date),
                        ("issue", payload.issue_date),
                    )
                    if event_date is not None
                ),
                source_url=snapshot.evidence[-1].url,
            ),
        )


def qualification_audit(
    checkpoint: DevonCheckpointV1,
    window: DiscoveryWindow,
) -> DevonQualificationAuditV1:
    """Project a validated terminal checkpoint without leaking pager rules."""
    validated = DevonCheckpointV1.model_validate_json(checkpoint.model_dump_json())
    scope = DevonDiscoveryScope(
        start=window.start,
        end=window.end,
        include_open=window.include_open,
    )
    if validated.live_scope != scope:
        _raise_checkpoint("qualification-scope-mismatch")
    expected = _query_keys(scope)
    return DevonQualificationAuditV1(
        scope=scope,
        expected_queries=expected,
        completed_queries=validated.completed_queries,
        query_summaries=validated.query_summaries,
        terminal_coherent=(
            validated.live_complete
            and validated.completed_queries == expected
            and validated.active_query is None
            and validated.next_page == 1
            and not validated.active_pages
        ),
        references=validated.seen_references,
    )


def _query_inventory(scope: DevonDiscoveryScope) -> tuple[_DevonQuery, ...]:
    planning_queries = (
        _DevonQuery(
            kind="received",
            key=f"received:{scope.start}:{scope.end}",
            start=scope.start,
            end=scope.end,
        ),
        _DevonQuery(
            kind="determined",
            key=f"determined:{scope.start}:{scope.end}",
            start=scope.start,
            end=scope.end,
        ),
    )
    appeal_queries = (
        _DevonQuery(
            kind="appeal-received",
            key=f"appeal-received:{scope.start}:{scope.end}",
            start=scope.start,
            end=scope.end,
        ),
        _DevonQuery(
            kind="appeal-determined",
            key=f"appeal-determined:{scope.start}:{scope.end}",
            start=scope.start,
            end=scope.end,
        ),
    )
    if not scope.include_open:
        return (*planning_queries, *appeal_queries)
    return (
        *planning_queries,
        _DevonQuery(kind="outstanding-planning", key="outstanding:planning:true"),
        *appeal_queries,
        _DevonQuery(kind="outstanding-appeals", key="outstanding:appeals:true"),
    )


def _query_keys(scope: DevonDiscoveryScope) -> tuple[str, ...]:
    return tuple(query.key for query in _query_inventory(scope))


def _validate_window(window: DiscoveryWindow) -> None:
    days = (window.end - window.start).days + 1
    if days < 1 or days > _MAX_WINDOW_DAYS:
        raise DevonWindowUnsupportedError(window.start, window.end)


async def _replay_committed_pages(
    session: PortalSession,
    progress: DevonCheckpointV1,
    first_page: _DiscoveryPage,
) -> _DiscoveryPage:
    current = first_page
    for index, expected in enumerate(progress.active_pages):
        if current.terminal or current.committed_proof() != expected:
            _raise_checkpoint("replay-mismatch")
        if index + 1 < len(progress.active_pages):
            current = await _fetch_result_page(
                session,
                current.committed_proof().next_locator,
                current.page + 1,
            )
    return await _fetch_result_page(
        session,
        current.committed_proof().next_locator,
        progress.next_page,
    )


async def _fetch_result_page(
    session: PortalSession,
    locator: HttpUrl,
    page: int,
) -> _DiscoveryPage:
    captures = await _fetch_protected(
        session,
        PortalRequest(url=locator, intent=RequestIntent.SEARCH),
    )
    return _parse_discovery_page(captures[-1].body, expected_page=page)


def _advance_checkpoint(
    progress: DevonCheckpointV1,
    *,
    query: _DevonQuery,
    page: _DiscoveryPage,
    all_query_keys: tuple[str, ...],
) -> tuple[DevonCheckpointV1, tuple[SourceReference, ...]]:
    expected_page = progress.next_page if progress.active_query == query.key else 1
    if page.page != expected_page:
        _raise_checkpoint("page-cursor-mismatch")
    if progress.active_pages:
        announced_pages = progress.active_pages[-1].numbered_pages
        if page.numbered_pages != announced_pages:
            _raise_checkpoint("pager-inventory-changed")
        if page.terminal != (page.page == announced_pages[-1]):
            _raise_checkpoint("pager-terminal-mismatch")
    active_references = {
        reference.reference
        for proof in progress.active_pages
        for reference in proof.references
    }
    seen = {item.reference: item for item in progress.seen_references}
    ordered = list(progress.seen_references)
    fresh = []
    for reference in page.references:
        if reference.reference in active_references:
            _raise_checkpoint("query-duplicate")
        prior = seen.get(reference.reference)
        if prior is not None and prior.locator != reference.locator:
            _raise_checkpoint("reference-locator-changed")
        if prior is None:
            seen[reference.reference] = reference
            ordered.append(reference)
            fresh.append(reference)
    if page.terminal:
        completed = (*progress.completed_queries, query.key)
        query_summaries = (
            *progress.query_summaries,
            DevonQuerySummaryV1(
                query_key=query.key,
                row_count=sum(len(proof.references) for proof in progress.active_pages)
                + len(page.references),
                page_count=page.page,
            ),
        )
        updated: dict[str, object] = {
            "completed_queries": completed,
            "query_summaries": query_summaries,
            "active_query": None,
            "next_page": 1,
            "active_pages": (),
            "seen_references": tuple(ordered),
            "live_complete": completed == all_query_keys,
        }
    else:
        updated = {
            "active_query": query.key,
            "next_page": page.page + 1,
            "active_pages": (*progress.active_pages, page.committed_proof()),
            "seen_references": tuple(ordered),
        }
    values = progress.model_dump()
    values.update(updated)
    return DevonCheckpointV1.model_validate(values), tuple(fresh)


def _parse_advanced_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    forms = soup.select("form#advancedSearchForm")
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_parse("advanced form")
    form = forms[0]
    action = urljoin(f"{BASE_URL}/", str(form.get("action", "")))
    if str(form.get("method", "")).casefold() != "post" or action != _RESULTS_URL:
        _raise_parse("advanced form action")
    controls = tuple(form.select("input[name], select[name], textarea[name]"))
    counts = Counter(
        str(control.get("name"))
        for control in controls
        if str(control.get("type", "")).casefold()
        not in {"button", "image", "reset", "submit"}
    )
    expected = Counter(
        {
            "__RequestVerificationToken": 1,
            "AdvancedSearch": 1,
            **dict.fromkeys(_BOOLEAN_FIELDS, 2),
            **dict.fromkeys(_VALUE_FIELDS, 1),
        }
    )
    if counts != expected:
        _raise_parse("advanced form controls")
    for name in _BOOLEAN_FIELDS:
        named = form.select(f'input[name="{name}"]')
        checkbox = tuple(
            item for item in named if str(item.get("type", "")).casefold() == "checkbox"
        )
        hidden = tuple(
            item for item in named if str(item.get("type", "")).casefold() == "hidden"
        )
        if (
            len(checkbox) != 1
            or str(checkbox[0].get("value", "")) != "true"
            or len(hidden) != 1
            or str(hidden[0].get("value", "")) != "false"
        ):
            _raise_parse("advanced boolean controls")
    return form


def _advanced_fields(  # noqa: C901
    form: Tag, query: _DevonQuery
) -> tuple[FormField, ...]:
    appeal_query = query.kind in {
        "appeal-received",
        "appeal-determined",
        "outstanding-appeals",
    }
    enabled = {
        "Outstanding": query.kind in {"outstanding-planning", "outstanding-appeals"},
        "SearchPlanning": not appeal_query,
        "SearchEnforcement": False,
        "SearchAppeals": appeal_query,
    }
    values = dict.fromkeys(_VALUE_FIELDS, "")
    if query.kind in {
        "received",
        "determined",
        "appeal-received",
        "appeal-determined",
    }:
        if query.start is None or query.end is None:
            _raise_checkpoint("dated-query-bounds")
        prefix = {
            "received": "DateReceived",
            "determined": "DateDetermined",
            "appeal-received": "DateAppeal",
            "appeal-determined": "DateAppealDecision",
        }[query.kind]
        values[f"{prefix}From"] = query.start.strftime("%d/%m/%Y")
        values[f"{prefix}To"] = query.end.strftime("%d/%m/%Y")
    fields = []
    for control in form.select("input[name], select[name], textarea[name]"):
        name = control.get("name")
        if not isinstance(name, str):
            continue
        control_type = str(control.get("type", "")).casefold()
        if control_type in {"button", "image", "reset", "submit"}:
            continue
        if control_type == "checkbox":
            if enabled[name]:
                fields.append(FormField(name=name, value=str(control.get("value", ""))))
            continue
        if name in values:
            value = values[name]
        elif control.name == "select":
            selected = control.select_one("option[selected]") or control.select_one(
                "option"
            )
            value = "" if selected is None else str(selected.get("value", ""))
        elif control.name == "textarea":
            value = control.get_text(strip=True)
        else:
            value = str(control.get("value", ""))
        fields.append(FormField(name=name, value=value))
    return tuple(fields)


def _parse_discovery_page(  # noqa: C901, PLR0912
    body: bytes, *, expected_page: int
) -> _DiscoveryPage:
    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(" ", strip=True)
    if _parse_disclaimer(body) is not None:
        _raise_parse("accepted disclaimer search response")
    result_blocks = soup.select("dl.searchResultsList")
    detail_blocks = soup.select("dl.details-grid")
    pagers = soup.select("ul.pagination")
    if result_blocks and detail_blocks:
        _raise_parse("mixed result and detail response")
    if detail_blocks:
        if pagers:
            _raise_pagination("singleton-detail-pager")
        if expected_page != 1:
            _raise_parse("page-one singleton detail")
        fields = _parse_labelled_fields(body)
        reference = _required_field(
            fields, "application number", "reference", "application reference"
        )
        routed = quote(reference, safe="/")
        return _DiscoveryPage(
            references=(
                SourceReference(
                    source_id=SOURCE,
                    reference=reference,
                    locator=f"{BASE_URL}/Planning/Display/{routed}",
                ),
            ),
            page=1,
            numbered_pages=(),
            numbered_links=(),
            next_locator=None,
            terminal=True,
        )
    references = tuple(_parse_result_reference(block) for block in result_blocks)
    if len({item.reference for item in references}) != len(references):
        _raise_parse("duplicate result reference")
    if not references:
        if pagers or "no records" not in text.casefold():
            _raise_parse("search results or no-records message")
        return _DiscoveryPage(
            references=(),
            page=expected_page,
            numbered_pages=(),
            numbered_links=(),
            next_locator=None,
            terminal=True,
        )
    if not pagers:
        if len(references) >= _PAGE_SIZE:
            _raise_pagination("full-page-without-pager")
        return _DiscoveryPage(
            references=references,
            page=expected_page,
            numbered_pages=(),
            numbered_links=(),
            next_locator=None,
            terminal=True,
        )
    if len(pagers) != 1:
        _raise_pagination("multiple-pagers")
    current, numbered_pages, numbered_links, next_locator = _parse_pager(pagers[0])
    if current != expected_page:
        _raise_pagination("current-page-mismatch")
    terminal = next_locator is None
    if not terminal and len(references) != _PAGE_SIZE:
        _raise_pagination("nonterminal-page-size")
    if terminal and not 1 <= len(references) <= _PAGE_SIZE:
        _raise_pagination("terminal-page-size")
    return _DiscoveryPage(
        references=references,
        page=current,
        numbered_pages=numbered_pages,
        numbered_links=numbered_links,
        next_locator=next_locator,
        terminal=terminal,
    )


def _parse_result_reference(block: Tag) -> SourceReference:
    link = block.select_one('a[href*="/Planning/Display/"]')
    if not isinstance(link, Tag):
        _raise_parse("search result detail link")
    href = str(link.get("href", ""))
    value = link.get_text(" ", strip=True)
    if not value:
        value = urlsplit(href).path.partition("/Planning/Display/")[2]
    locator = urljoin(f"{BASE_URL}/", href)
    parts = urlsplit(locator)
    if (
        parts.scheme != "https"
        or parts.netloc != urlsplit(BASE_URL).netloc
        or not parts.path.startswith("/Planning/Display/")
        or parts.query
        or parts.fragment
    ):
        _raise_parse("search result detail locator")
    return SourceReference(source_id=SOURCE, reference=value, locator=locator)


def _parse_pager(  # noqa: C901, PLR0912
    pager: Tag,
) -> tuple[int, tuple[int, ...], tuple[DevonPageLinkV1, ...], HttpUrl | None]:
    current_markers = []
    for item in pager.select("li"):
        classes = {str(value).casefold() for value in item.get_attribute_list("class")}
        aria_current = str(item.get("aria-current", "")).casefold() == "page"
        if "active" in classes or aria_current:
            value = item.get_text(" ", strip=True)
            if value.isdigit():
                current_markers.append(int(value))
    if len(current_markers) != 1:
        _raise_pagination("current-page-marker")
    current = current_markers[0]
    links = []
    next_links = []
    for link in pager.select("a[href]"):
        label = link.get_text(" ", strip=True)
        locator = HttpUrl(urljoin(f"{BASE_URL}/", str(link.get("href", ""))))
        if label.isdigit():
            page = int(label)
            if _page_from_locator(locator) != page:
                _raise_pagination("numbered-locator")
            links.append(DevonPageLinkV1(page=page, locator=locator))
        elif label.casefold() in {"next", ">", "»"} or "next" in tuple(
            str(value).casefold() for value in link.get_attribute_list("rel")
        ):
            next_links.append(locator)
    if len({link.page for link in links}) != len(links):
        _raise_pagination("duplicate-numbered-link")
    numbered_pages = tuple(sorted((current, *(link.page for link in links))))
    if any(link.page == current for link in links):
        _raise_pagination("linked-current-page")
    if numbered_pages != tuple(range(1, max(numbered_pages) + 1)):
        _raise_pagination("nonconsecutive-numbering")
    if len(next_links) > 1:
        _raise_pagination("duplicate-forward-link")
    if current < numbered_pages[-1]:
        if len(next_links) != 1 or _page_from_locator(next_links[0]) != current + 1:
            _raise_pagination("nonterminal-forward-link")
        next_locator: HttpUrl | None = next_links[0]
    else:
        if next_links:
            _raise_pagination("terminal-forward-link")
        next_locator = None
    return (
        current,
        numbered_pages,
        tuple(sorted(links, key=lambda item: item.page)),
        next_locator,
    )


def _page_from_locator(locator: HttpUrl) -> int:
    parts = urlsplit(str(locator))
    if parts.scheme != "https" or parts.netloc != urlsplit(BASE_URL).netloc:
        _raise_pagination("pager-host")
    if parts.query or parts.fragment:
        _raise_pagination("pager-parameters")
    if parts.path.rstrip("/") == "/Search/Results":
        return 1
    match = re.fullmatch(r"/Search/Results/(\d+)", parts.path)
    if match is None or int(match.group(1)) < _FIRST_PAGED_RESULT:
        _raise_pagination("pager-locator")
    return int(match.group(1))


def _snapshot(
    reference: SourceReference,
    payload: DevonApplicationV1,
    evidence: tuple[EvidenceCapture, ...],
    document_state: CompleteSection | UnavailableSection,
) -> NativeSnapshot[DevonApplicationV1]:
    return NativeSnapshot(
        reference=reference,
        observed_at=datetime.now(UTC),
        payload=payload,
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=document_state,
            comments=UnavailableSection(
                reason="Devon responses are published only as document attachments"
            ),
        ),
        evidence=evidence,
    )


async def _fetch_protected(
    session: PortalSession,
    request: PortalRequest,
) -> tuple[EvidenceCapture, ...]:
    request = request.model_copy(update={"redirect_boundary": _REDIRECT_BOUNDARY})
    _validate_protected_request(request)
    first = await session.fetch(request)
    disclaimer = _parse_disclaimer(first.body)
    if disclaimer is None:
        return (first,)
    action, fields = disclaimer
    accepted_url = HttpUrl(urljoin(f"{BASE_URL}/", action))
    _validate_disclaimer_action(accepted_url)
    accepted = await session.fetch(
        PortalRequest(
            url=accepted_url,
            intent=request.intent,
            method=RequestMethod.POST,
            form=fields,
            redirect_boundary=_REDIRECT_BOUNDARY,
        )
    )
    if _parse_disclaimer(accepted.body) is not None:
        raise DevonDisclaimerAcceptanceError
    return first, accepted


def _validate_protected_request(request: PortalRequest) -> None:
    parts = urlsplit(str(request.url))
    expected_origin = urlsplit(BASE_URL)
    if (
        parts.scheme,
        parts.hostname,
        parts.port,
        parts.username,
        parts.password,
    ) != (
        expected_origin.scheme,
        expected_origin.hostname,
        expected_origin.port,
        None,
        None,
    ):
        _raise_protected("request-origin")
    allowed_paths = {
        (RequestIntent.SEARCH, RequestMethod.GET): re.compile(
            r"/Search/(?:Advanced|Results(?:/(?:[2-9]|[1-9]\d+))?)"
        ),
        (RequestIntent.SEARCH, RequestMethod.POST): re.compile(r"/Search/Results"),
        (RequestIntent.DETAIL, RequestMethod.GET): re.compile(r"/Planning/Display/.+"),
    }
    pattern = allowed_paths.get((request.intent, request.method))
    if (
        pattern is None
        or pattern.fullmatch(parts.path) is None
        or parts.query
        or parts.fragment
    ):
        _raise_protected("request-path")


def _validate_disclaimer_action(url: HttpUrl) -> None:
    parts = urlsplit(str(url))
    expected_origin = urlsplit(BASE_URL)
    if (
        parts.scheme,
        parts.hostname,
        parts.port,
        parts.username,
        parts.password,
        parts.path,
        parts.fragment,
    ) != (
        expected_origin.scheme,
        expected_origin.hostname,
        expected_origin.port,
        None,
        None,
        "/Disclaimer/Accept",
        "",
    ):
        _raise_protected("disclaimer-action")


def _parse_disclaimer(body: bytes) -> tuple[str, tuple[FormField, ...]] | None:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one('form[action*="Disclaimer/Accept"]')
    if not isinstance(form, Tag):
        return None
    fields = tuple(
        FormField(name=str(item.get("name")), value=str(item.get("value", "")))
        for item in form.select("input[name]")
        if str(item.get("type", "")).casefold() not in {"button", "submit"}
    )
    return str(form.get("action", "")), fields


def _parse_labelled_fields(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for block in soup.select("dl.details-grid"):
        for term in block.select("dt"):
            value = term.find_next_sibling("dd")
            if isinstance(value, Tag):
                fields[_normalise_label(term.get_text(" ", strip=True))] = (
                    _leading_text(value)
                )
    if not fields:
        _raise_parse("details-grid")
    return fields


def _leading_text(value: Tag) -> str:
    """Read a value only until Devon's first unclosed nested label."""
    return " ".join(next(value.stripped_strings, "").split())


def _parse_coordinates(body: bytes) -> tuple[float | None, float | None]:
    """Retain a complete BNG pair from the source-owned map script."""
    text = body.decode(errors="replace")
    easting = re.search(r"\bvar\s+easting\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;", text)
    northing = re.search(r"\bvar\s+northing\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;", text)
    if easting is None and northing is None:
        return None, None
    if easting is None or northing is None:
        _raise_parse("coordinates")
    return float(easting.group(1)), float(northing.group(1))


def _parse_constraints(body: bytes) -> tuple[tuple[str, ...], bool]:
    """Enumerate the optional constraint table when the source exposes it."""
    soup = BeautifulSoup(body, "html.parser")
    tables = soup.select('table[summary="Planning Constraints"]')
    if not tables:
        return (), False
    if len(tables) != 1:
        _raise_parse("constraints table")
    headers = tuple(
        _normalise_label(item.get_text(" ", strip=True))
        for item in tables[0].select("thead th")
    )
    if headers != ("description",):
        _raise_parse("constraints headers")
    constraints = []
    for row in tables[0].select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 1 or not (value := cells[0].get_text(" ", strip=True)):
            _raise_parse("constraint row")
        constraints.append(value)
    return tuple(constraints), True


def _parse_consultations(
    body: bytes,
) -> tuple[tuple[DevonConsultationV1, ...], bool]:
    """Retain every optional consultee row, including malformed source rows."""
    soup = BeautifulSoup(body, "html.parser")
    tables = soup.select('table[summary="Planning Consultees"]')
    if not tables:
        return (), False
    if len(tables) != 1:
        _raise_parse("consultations table")
    headers = tuple(
        _normalise_label(item.get_text(" ", strip=True))
        for item in tables[0].select("thead th")
    )
    if headers != (
        "consultee name",
        "date letter sent",
        "consultation expiry date",
        "reply received",
    ):
        _raise_parse("consultations headers")
    rows = []
    for row in tables[0].select("tbody tr"):
        values = tuple(
            value
            for cell in row.find_all("td", recursive=False)
            if (value := cell.get_text(" ", strip=True))
        )
        if not values:
            _raise_parse("consultation row")
        rows.append(DevonConsultationV1(values=values))
    return tuple(rows), True


def _parse_documents(
    body: bytes,
) -> tuple[tuple[DevonDocumentV1, ...], CompleteSection | UnavailableSection]:
    soup = BeautifulSoup(body, "html.parser")
    markers = soup.select("div#PlanningdocTable")
    tables = soup.select("table.document-list")
    if not markers and not tables:
        return (), UnavailableSection(reason="Devon document section is not exposed")
    if len(markers) != 1 or len(tables) != 1:
        _raise_parse("document section")
    headers = tuple(
        _normalise_label(_leading_text(item))
        for item in tables[0].select("thead th")
    )
    if len(headers) < _DOCUMENT_COLUMN_COUNT or headers[-2:] != (
        "description",
        "created date",
    ):
        _raise_parse("document headers")
    documents = [
        document
        for group in tables[0].select("tbody")
        for document in _parse_document_group(group)
    ]
    if not documents:
        _raise_parse("document rows")
    items = tuple(documents)
    return items, CompleteSection(item_count=len(items))


def _parse_document_group(group: Tag) -> tuple[DevonDocumentV1, ...]:
    category_rows = group.select("tr.header")
    data_rows = tuple(
        row for row in group.find_all("tr", recursive=False) if row not in category_rows
    )
    if len(category_rows) != 1 or not data_rows:
        _raise_parse("document group")
    category = category_rows[0].get_text(" ", strip=True)
    if not category:
        _raise_parse("document category")
    documents = []
    for row in data_rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) != _DOCUMENT_COLUMN_COUNT:
            _raise_parse("document row")
        links = cells[1].select("a[href]")
        if len(links) != 1:
            _raise_parse("document row")
        link = links[0]
        href = urljoin(f"{BASE_URL}/", str(link.get("href", "")))
        parts = urlsplit(href)
        if (
            parts.scheme != "https"
            or parts.netloc != urlsplit(BASE_URL).netloc
            or parts.path != "/Document/Download"
            or parts.fragment
        ):
            _raise_parse("document locator")
        query = parse_qs(parts.query)
        title = (
            link.get_text(" ", strip=True)
            or _query_value(query, "fileName", "filename")
            or "Document"
        )
        created = cells[2].get_text(" ", strip=True)
        documents.append(
            DevonDocumentV1(
                title=title,
                url=HttpUrl(href),
                module=_query_value(query, "module"),
                record_number=_query_value(query, "recordNumber", "record"),
                plan_identifier=_query_value(query, "planId", "plan"),
                image_identifier=_query_value(query, "imageId", "image"),
                is_plan=_query_bool(query, "isPlan"),
                filename=_query_value(query, "fileName", "filename"),
                category=category,
                published_date=_parse_date(created, "document date"),
            )
        )
    return tuple(documents)


def _split_lines(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(item.strip() for item in value.splitlines() if item.strip())


def _query_value(values: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        found = values.get(name)
        if found and found[0]:
            return found[0]
    return None


def _query_bool(values: dict[str, list[str]], name: str) -> bool | None:
    value = _query_value(values, name)
    if value is None:
        return None
    if value.casefold() == "true":
        return True
    if value.casefold() == "false":
        return False
    return _raise_parse(f"document {name}")


def _normalise_label(value: str) -> str:
    return " ".join(value.strip().rstrip(":").casefold().split())


def _optional_field(fields: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = fields.get(_normalise_label(name))
        if value:
            return unescape(value)
    return None


def _required_field(fields: dict[str, str], *names: str) -> str:
    value = _optional_field(fields, *names)
    if value is None:
        _raise_parse(f"detail {'/'.join(names)}")
    return value


def _optional_date(fields: dict[str, str], *names: str) -> date | None:
    value = _optional_field(fields, *names)
    if value is None or value == "-":
        return None
    return _parse_date(value, "/".join(names))


def _parse_date(value: str, field: str) -> date:
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return _raise_parse(f"date {field}")


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class DevonParseError(ValueError):
    """A required Devon boundary value was absent or inconsistent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Devon field {field}")


class DevonWindowUnsupportedError(ValueError):
    """The requested inclusive live interval exceeds Devon's safe bound."""

    def __init__(self, start: date, end: date) -> None:
        """Report the rejected interval and supported maximum."""
        super().__init__(
            "Devon live discovery requires 1 through "
            f"{_MAX_WINDOW_DAYS} inclusive days; "
            f"received {start} through {end}"
        )


class DevonPaginationError(DevonParseError):
    """Devon's observable pager cannot prove complete enumeration."""


class DevonDisclaimerAcceptanceError(RuntimeError):
    """The protected route remained behind its disclaimer."""


class DevonCheckpointError(ValueError):
    """A saved cursor conflicts with the requested or replayed scope."""


class DevonRoutingError(ValueError):
    """A reference lacks Devon's detail locator."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Devon cannot route reference {reference}")


class DevonProtectedRouteError(ValueError):
    """A live request falls outside Devon's observed official routes."""


class DevonReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(f"expected Devon reference {expected}, received {actual}")


def _raise_parse(field: str) -> NoReturn:
    raise DevonParseError(field)


def _raise_pagination(code: str) -> NoReturn:
    raise DevonPaginationError(code)


def _raise_checkpoint(code: str) -> NoReturn:
    raise DevonCheckpointError(code)


def _raise_protected(code: str) -> NoReturn:
    raise DevonProtectedRouteError(code)
