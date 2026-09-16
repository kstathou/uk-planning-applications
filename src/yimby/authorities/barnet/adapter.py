# Copyright (c) 2026 Kostas Stathoulopoulos

"""Barnet fixture and authority-native IDOX collection."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl

from yimby.domain import (
    ApplicationMetadata,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CommentRecord,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    EmptySection,
    FailedSection,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SectionState,
    SourceDefinition,
    SourceId,
    SourceReference,
    UnavailableSection,
    collection_state,
)
from yimby.transport import (
    FormField,
    PortalRequest,
    RequestIntent,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalSession

CURRENT_SOURCE = SourceId("barnet-idox-current")
BASE_URL = "https://publicaccess.barnet.gov.uk/online-applications"
_WEEKLY_FORM_URL = f"{BASE_URL}/search.do?action=weeklyList"
_WEEKLY_RESULTS_URL = f"{BASE_URL}/weeklyListResults.do?action=firstPage"
_ADVANCED_FORM_URL = f"{BASE_URL}/search.do?action=advanced"
_ADVANCED_RESULTS_URL = f"{BASE_URL}/advancedSearchResults.do?action=firstPage"
_PAGED_RESULTS_URL = f"{BASE_URL}/pagedSearchResults.do"
type _DateType = Literal["DC_Validated", "DC_Decided"]


_DATE_TYPES: tuple[_DateType, ...] = ("DC_Validated", "DC_Decided")
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_MINIMUM_LABELLED_CELLS = 2


class BarnetDiscoveryScope(FrozenModel):
    """Exact live discovery request owning resumable Barnet progress."""

    start: date
    end: date
    include_open: bool


class BarnetCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable live discovery progress."""

    cursor: str
    live_scope: BarnetDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    query_row_count: int = 0
    query_reported_count: int | None = None
    active_query_references: tuple[str, ...] = ()
    seen_references: tuple[str, ...] = ()
    seen_locators: tuple[str | None, ...] = ()
    tracks_locators: bool = False
    live_complete: bool = False


class _WeeklyQuery(FrozenModel):
    week_start: date
    portal_week: str
    date_type: _DateType

    @property
    def key(self) -> str:
        return f"weekly|{self.week_start.isoformat()}|{self.date_type}"


class _OpenCaseQuery(FrozenModel):
    kind: Literal["open-case"] = "open-case"
    value: Literal[
        "Application Received",
        "Valid Application Received",
        "Pending Consideration",
        "Pending Decision",
    ]

    @property
    def field(self) -> str:
        return "searchCriteria.caseStatus"

    @property
    def key(self) -> str:
        return f"advanced|{self.field}|{self.value}"


class _ActiveAppealQuery(FrozenModel):
    kind: Literal["active-appeal"] = "active-appeal"
    value: Literal[
        "Appeal in progress",
        "Appeal lodged",
        "Appeal Valid",
        "High Court Appeal Lodged",
        "Remitted to Secretary of State",
    ]

    @property
    def field(self) -> str:
        return "searchCriteria.appealStatus"

    @property
    def key(self) -> str:
        return f"advanced|{self.field}|{self.value}"


class _ReceivedDateQuery(FrozenModel):
    kind: Literal["received-date"] = "received-date"
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"advanced|received|{self.start.isoformat()}|{self.end.isoformat()}"


type _StatusQuery = _OpenCaseQuery | _ActiveAppealQuery
type _AdvancedQuery = _ReceivedDateQuery | _StatusQuery


_STATUS_QUERIES: tuple[_StatusQuery, ...] = (
    _OpenCaseQuery(value="Application Received"),
    _OpenCaseQuery(value="Valid Application Received"),
    _OpenCaseQuery(value="Pending Consideration"),
    _OpenCaseQuery(value="Pending Decision"),
    _ActiveAppealQuery(value="Appeal in progress"),
    _ActiveAppealQuery(value="Appeal lodged"),
    _ActiveAppealQuery(value="Appeal Valid"),
    _ActiveAppealQuery(value="High Court Appeal Lodged"),
    _ActiveAppealQuery(value="Remitted to Secretary of State"),
)


def _intersecting_mondays(scope: BarnetDiscoveryScope) -> tuple[date, ...]:
    monday = scope.start - timedelta(days=scope.start.weekday())
    weeks = []
    while monday <= scope.end:
        weeks.append(monday)
        monday += timedelta(days=7)
    return tuple(weeks)


def expected_live_query_keys(scope: BarnetDiscoveryScope) -> tuple[str, ...]:
    """Return the exact canonical query inventory for one Barnet scope."""
    weekly = tuple(
        f"weekly|{monday.isoformat()}|{date_type}"
        for monday in _intersecting_mondays(scope)
        for date_type in _DATE_TYPES
    )
    advanced = tuple(query.key for query in _advanced_queries(scope))
    return weekly + advanced


def _advanced_queries(scope: BarnetDiscoveryScope) -> tuple[_AdvancedQuery, ...]:
    received = _ReceivedDateQuery(start=scope.start, end=scope.end)
    return (received, *_STATUS_QUERIES) if scope.include_open else (received,)


def _is_terminal_checkpoint(
    checkpoint: BarnetCheckpointV1,
    scope: BarnetDiscoveryScope,
) -> bool:
    return (
        checkpoint.cursor == "live"
        and checkpoint.live_scope == scope
        and checkpoint.completed_queries == expected_live_query_keys(scope)
        and checkpoint.active_query is None
        and checkpoint.next_page == 1
        and checkpoint.query_row_count == 0
        and checkpoint.query_reported_count is None
        and not checkpoint.active_query_references
        and len(checkpoint.seen_references) == len(set(checkpoint.seen_references))
        and len(checkpoint.seen_locators) <= len(checkpoint.seen_references)
        and (
            not checkpoint.tracks_locators
            or (
                len(checkpoint.seen_locators) == len(checkpoint.seen_references)
                and all(locator is not None for locator in checkpoint.seen_locators)
            )
        )
    )


class BarnetDocumentV1(FrozenModel):
    """Barnet document metadata without attachment content."""

    title: str
    url: HttpUrl
    published_date: date | None = None
    document_type: str | None = None
    drawing_number: str | None = None
    description: str | None = None


class BarnetCommentV1(FrozenModel):
    """One public or consultee comment exposed by Barnet."""

    comment_id: str
    text: str
    category: str = "public"


class BarnetApplicationV1(FrozenModel):
    """Barnet-native application payload."""

    proposal: str
    status: str
    documents: tuple[BarnetDocumentV1, ...]
    comments: tuple[BarnetCommentV1, ...]
    alternative_reference: str | None = None
    application_type: str | None = None
    decision: str | None = None
    address: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision_date: date | None = None
    officer_name: str | None = None
    document_state: SectionState | None = None
    public_comment_state: SectionState | None = None
    consultee_comment_state: SectionState | None = None


class BarnetAdapter:
    """Collect Barnet records while keeping IDOX rules authority-local."""

    manifest = AuthorityManifest(
        id=AuthorityId("barnet"),
        name="London Borough of Barnet",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(
            SourceDefinition(
                id=SourceId("barnet-council-entry"),
                base_url=HttpUrl(
                    "https://www.barnet.gov.uk/planning-and-building-control/"
                    "planning-applications-and-permissions/"
                    "view-search-and-comment"
                ),
                valid_from=date(2026, 9, 15),
            ),
            SourceDefinition(
                id=CURRENT_SOURCE,
                base_url=HttpUrl(f"{BASE_URL}/"),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BarnetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BarnetCheckpointV1]]:
        """Use fixture discovery or the captured live IDOX weekly flow."""
        if session.mode.value == "fixture":
            async for batch in self._discover_fixture(session, window, checkpoint):
                yield batch
            return
        async for batch in self._discover_live(session, window, checkpoint):
            yield batch

    async def _discover_fixture(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BarnetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BarnetCheckpointV1]]:
        cursor = "start" if checkpoint is None else checkpoint.cursor
        url = (
            f"{BASE_URL}/search?start={window.start.isoformat()}"
            f"&end={window.end.isoformat()}&cursor={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=CURRENT_SOURCE, reference=value)
            for value in re.findall(r'data-reference="([^"]+)"', html)
        )
        next_cursor = _required(html, r'data-next-cursor="([^"]+)"')
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=BarnetCheckpointV1(cursor=next_cursor),
            complete=next_cursor == "complete",
        )

    async def _discover_live(  # noqa: C901, PLR0912, PLR0915
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BarnetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BarnetCheckpointV1]]:
        requested_scope = BarnetDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != requested_scope:
            progress = BarnetCheckpointV1(
                cursor="live",
                live_scope=requested_scope,
                tracks_locators=True,
            )
        if progress.live_complete:
            if not _is_terminal_checkpoint(progress, requested_scope):
                terminal_error = "terminal checkpoint"
                raise BarnetCheckpointError(terminal_error)
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=progress,
                complete=True,
            )
            return
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_WEEKLY_FORM_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_form(form_capture.body)
        weekly_queries = _weekly_queries(form, requested_scope)
        query_keys = expected_live_query_keys(requested_scope)
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            raise BarnetCheckpointError(progress.active_query)
        pending_weekly = tuple(
            query
            for query in weekly_queries
            if query.key not in progress.completed_queries
        )
        for weekly_query in pending_weekly:
            page = (
                progress.next_page if progress.active_query == weekly_query.key else 1
            )
            row_count = (
                progress.query_row_count
                if progress.active_query == weekly_query.key
                else 0
            )
            if progress.active_query == weekly_query.key and page > 1:
                prior_page_list = []
                for prior_page in range(1, page):
                    prior_page_list.append(
                        _parse_search_page(
                            (
                                await session.fetch(
                                    _weekly_request(form, weekly_query, prior_page)
                                )
                            ).body
                        )
                    )
                prior_pages = tuple(prior_page_list)
                progress = _restore_query_progress(
                    progress,
                    weekly_query.key,
                    prior_pages,
                )
            while True:
                capture = await session.fetch(_weekly_request(form, weekly_query, page))
                search_page = _parse_search_page(capture.body)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=weekly_query.key,
                        page=page,
                        row_count=row_count,
                    ),
                    search_page=search_page,
                    all_query_keys=query_keys,
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                )
                progress = next_checkpoint
                if last_page:
                    break
                row_count = next_checkpoint.query_row_count
                page += 1
        advanced_form = _parse_advanced_form(
            (
                await session.fetch(
                    PortalRequest(
                        url=HttpUrl(_ADVANCED_FORM_URL),
                        intent=RequestIntent.SEARCH,
                    )
                )
            ).body
        )
        pending_advanced = tuple(
            query
            for query in _advanced_queries(requested_scope)
            if query.key not in progress.completed_queries
        )
        for advanced_query in pending_advanced:
            page = (
                progress.next_page if progress.active_query == advanced_query.key else 1
            )
            row_count = (
                progress.query_row_count
                if progress.active_query == advanced_query.key
                else 0
            )
            if progress.active_query == advanced_query.key and page > 1:
                prior_page_list = []
                for prior_page in range(1, page):
                    prior_page_list.append(
                        _parse_advanced_search_page(
                            (
                                await session.fetch(
                                    _advanced_request(
                                        advanced_form,
                                        advanced_query,
                                        prior_page,
                                    )
                                )
                            ).body,
                            page=prior_page,
                        )
                    )
                prior_pages = tuple(prior_page_list)
                progress = _restore_query_progress(
                    progress,
                    advanced_query.key,
                    prior_pages,
                )
            while True:
                capture = await session.fetch(
                    _advanced_request(advanced_form, advanced_query, page)
                )
                search_page = _parse_advanced_search_page(capture.body, page=page)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=advanced_query.key,
                        page=page,
                        row_count=row_count,
                    ),
                    search_page=search_page,
                    all_query_keys=query_keys,
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                )
                progress = next_checkpoint
                if last_page:
                    break
                row_count = next_checkpoint.query_row_count
                page += 1
        if not pending_advanced:
            completed_checkpoint = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=completed_checkpoint,
                complete=True,
            )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BarnetApplicationV1]:
        """Fetch fixture detail or live IDOX summary and child sections."""
        if session.mode.value == "fixture":
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BarnetApplicationV1]:
        detail_url = f"{BASE_URL}/application/{quote(reference.reference)}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(detail_url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        documents = tuple(
            BarnetDocumentV1(title=unescape(title), url=HttpUrl(url))
            for title, url in re.findall(
                r'data-document-title="([^"]+)" data-document-url="([^"]+)"',
                html,
            )
        )
        comments_url = _required(html, r'data-comments-url="([^"]+)"')
        evidence = [detail]
        comments_state: CompleteSection | EmptySection | FailedSection
        try:
            comments_capture = await session.fetch(
                PortalRequest(
                    url=HttpUrl(comments_url),
                    intent=RequestIntent.COMMENTS,
                )
            )
        except SourceUnavailableError:
            comments: tuple[BarnetCommentV1, ...] = ()
            comments_state = FailedSection(code="source-unavailable")
        else:
            evidence.append(comments_capture)
            comments = tuple(
                BarnetCommentV1(
                    comment_id=comment_id,
                    text=unescape(text.strip()),
                )
                for comment_id, text in re.findall(
                    r'data-comment-id="([^"]+)">([^<]+)</li>',
                    comments_capture.body.decode(),
                )
            )
            comments_state = collection_state(len(comments))
        documents_state = collection_state(len(documents))
        payload = BarnetApplicationV1(
            proposal=unescape(_required(html, r'id="proposal">([^<]+)</')),
            status=unescape(_required(html, r'id="status">([^<]+)</')),
            documents=documents,
            comments=comments,
            document_state=documents_state,
            public_comment_state=comments_state,
            consultee_comment_state=UnavailableSection(
                reason="not exposed by the deterministic unit-one fixture"
            ),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=documents_state,
                comments=comments_state,
            ),
            evidence=tuple(evidence),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BarnetApplicationV1]:
        if reference.locator is None:
            raise BarnetRoutingError(reference.reference)
        summary = await session.fetch(
            _detail_request(reference.locator, "summary", RequestIntent.DETAIL)
        )
        fields = _parse_summary(summary.body)
        published_reference = _required_field(fields, "reference")
        if published_reference != reference.reference:
            raise BarnetReferenceMismatchError(
                reference.reference,
                published_reference,
            )
        evidence: list[EvidenceCapture] = [summary]
        documents, document_state = await _fetch_documents(
            session,
            reference.locator,
            evidence,
        )
        public_comments, public_state = await _fetch_comments(
            session,
            reference.locator,
            _CommentTab(
                active_tab="neighbourComments",
                category="public",
                count_labels=("public comments", "neighbour comments"),
            ),
            evidence,
        )
        consultee_comments, consultee_state = await _fetch_comments(
            session,
            reference.locator,
            _CommentTab(
                active_tab="consulteeComments",
                category="consultee",
                count_labels=("consultee comments", "consultee responses"),
            ),
            evidence,
        )
        comments = (*public_comments, *consultee_comments)
        comments_state = _combined_comment_state(
            public_state,
            consultee_state,
            len(comments),
        )
        payload = BarnetApplicationV1(
            proposal=_required_field(fields, "proposal", "description"),
            status=_required_field(fields, "status"),
            documents=documents,
            comments=comments,
            alternative_reference=_optional_field(fields, "alternative reference"),
            application_type=_optional_field(fields, "application type"),
            decision=_optional_field(fields, "decision"),
            address=_optional_field(fields, "address"),
            received_date=_optional_date(fields, "received date"),
            validated_date=_optional_date(fields, "validated date"),
            decision_date=_optional_date(fields, "decision date"),
            officer_name=_optional_field(fields, "case officer", "officer"),
            document_state=document_state,
            public_comment_state=public_state,
            consultee_comment_state=consultee_state,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=document_state,
                comments=comments_state,
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[BarnetApplicationV1],
    ) -> NormalisedObservation:
        """Map Barnet-native values and metadata to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        provenance_fields = ["proposal", "status"]
        for field, value in (
            ("address", payload.address),
            ("application_type", payload.application_type),
            ("decision", payload.decision),
            ("received_date", payload.received_date),
            ("validated_date", payload.validated_date),
            ("decision_date", payload.decision_date),
            ("officer_name", payload.officer_name),
        ):
            if value is not None:
                provenance_fields.append(field)
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal,
            status=payload.status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in payload.documents
            ),
            comments=tuple(
                CommentRecord(comment_id=item.comment_id, text=item.text)
                for item in payload.comments
            ),
            completeness=snapshot.completeness,
            provenance=tuple(
                Provenance(field=field, evidence=evidence)
                for field in provenance_fields
            ),
            normaliser_version="barnet-v2",
            metadata=ApplicationMetadata(
                aliases=(
                    ()
                    if payload.alternative_reference is None
                    else (payload.alternative_reference,)
                ),
                application_type=payload.application_type,
                decision=payload.decision,
                address=payload.address,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                officer_name=payload.officer_name,
            ),
        )


class _SearchPage(FrozenModel):
    references: tuple[SourceReference, ...]
    reported: int
    displayed_range: tuple[int, int] | None = None


class _ActivePage(FrozenModel):
    query_key: str
    page: int
    row_count: int


class _CommentTab(FrozenModel):
    active_tab: str
    category: str
    count_labels: tuple[str, ...]


def _advance_checkpoint(
    progress: BarnetCheckpointV1,
    *,
    active_page: _ActivePage,
    search_page: _SearchPage,
    all_query_keys: tuple[str, ...],
) -> tuple[BarnetCheckpointV1, tuple[SourceReference, ...], bool]:
    if (
        progress.query_reported_count is not None
        and progress.query_reported_count != search_page.reported
    ):
        raise BarnetCountMismatchError(
            active_page.query_key,
            progress.query_reported_count,
            search_page.reported,
        )
    next_row_count = active_page.row_count + len(search_page.references)
    if next_row_count > search_page.reported or (
        not search_page.references and next_row_count < search_page.reported
    ):
        raise BarnetCountMismatchError(
            active_page.query_key,
            search_page.reported,
            next_row_count,
        )
    if search_page.displayed_range is None:
        if active_page.row_count or next_row_count < search_page.reported:
            _raise_parse("displayed result range")
    elif search_page.displayed_range != (
        active_page.row_count + 1,
        next_row_count,
    ):
        _raise_parse("displayed result range")
    active_references = _active_query_references(progress, active_page)
    active_seen = set(active_references)
    for reference in search_page.references:
        if reference.reference in active_seen:
            _raise_parse("duplicate search result identity")
        active_seen.add(reference.reference)
    next_active_references = (
        *active_references,
        *(reference.reference for reference in search_page.references),
    )
    seen, locators, fresh = _reconcile_search_identities(
        progress,
        search_page.references,
    )
    last_page = next_row_count == search_page.reported
    if last_page:
        completed_queries = (*progress.completed_queries, active_page.query_key)
        checkpoint = progress.model_copy(
            update={
                "completed_queries": completed_queries,
                "active_query": None,
                "next_page": 1,
                "query_row_count": 0,
                "query_reported_count": None,
                "active_query_references": (),
                "seen_references": seen,
                "seen_locators": locators,
                "live_complete": len(completed_queries) == len(all_query_keys),
            }
        )
    else:
        checkpoint = progress.model_copy(
            update={
                "active_query": active_page.query_key,
                "next_page": active_page.page + 1,
                "query_row_count": next_row_count,
                "query_reported_count": search_page.reported,
                "active_query_references": next_active_references,
                "seen_references": seen,
                "seen_locators": locators,
            }
        )
    return checkpoint, fresh, last_page


def _active_query_references(
    progress: BarnetCheckpointV1,
    active_page: _ActivePage,
) -> tuple[str, ...]:
    if active_page.row_count == 0:
        return ()
    references = progress.active_query_references
    if (
        not references
        and not progress.tracks_locators
        and not progress.completed_queries
        and active_page.row_count == len(progress.seen_references)
    ):
        references = progress.seen_references
    if (
        progress.active_query != active_page.query_key
        or len(references) != active_page.row_count
        or len(references) != len(set(references))
    ):
        checkpoint_error = "active query identities"
        raise BarnetCheckpointError(checkpoint_error)
    return references


def _restore_query_progress(
    progress: BarnetCheckpointV1,
    query_key: str,
    prior_pages: tuple[_SearchPage, ...],
) -> BarnetCheckpointV1:
    active_page = _ActivePage(
        query_key=query_key,
        page=progress.next_page,
        row_count=progress.query_row_count,
    )
    active_references = _active_query_references(progress, active_page)
    known_locators = dict(
        zip(progress.seen_references, progress.seen_locators, strict=False)
    )
    restored_references = []
    restored_row_count = 0
    reported_count = progress.query_reported_count
    for search_page in prior_pages:
        if reported_count is None:
            reported_count = search_page.reported
        elif reported_count != search_page.reported:
            raise BarnetCountMismatchError(
                query_key,
                reported_count,
                search_page.reported,
            )
        next_row_count = restored_row_count + len(search_page.references)
        if search_page.displayed_range != (restored_row_count + 1, next_row_count):
            _raise_parse("resumed search result identity")
        for reference in search_page.references:
            known_locator = known_locators.get(reference.reference)
            if known_locator is not None and known_locator != reference.locator:
                _raise_parse("resumed search result identity")
            restored_references.append(reference.reference)
        restored_row_count = next_row_count
    if (
        restored_row_count != progress.query_row_count
        or tuple(restored_references) != active_references
        or reported_count is None
    ):
        _raise_parse("resumed search result identity")
    return progress.model_copy(
        update={
            "query_reported_count": reported_count,
            "active_query_references": active_references,
        }
    )


def _reconcile_search_identities(
    progress: BarnetCheckpointV1,
    references: tuple[SourceReference, ...],
) -> tuple[tuple[str, ...], tuple[str | None, ...], tuple[SourceReference, ...]]:
    if (
        len(progress.seen_locators) > len(progress.seen_references)
        or len(progress.seen_references) != len(set(progress.seen_references))
        or (
            progress.tracks_locators
            and len(progress.seen_locators) != len(progress.seen_references)
        )
    ):
        identity_error = "seen result identities"
        raise BarnetCheckpointError(identity_error)
    seen = list(progress.seen_references)
    locators = [
        *progress.seen_locators,
        *([None] * (len(seen) - len(progress.seen_locators))),
    ]
    positions = {reference: index for index, reference in enumerate(seen)}
    fresh = []
    for reference in references:
        if reference.locator is None:
            _raise_parse("search result identity")
        position = positions.get(reference.reference)
        if position is None:
            positions[reference.reference] = len(seen)
            seen.append(reference.reference)
            locators.append(reference.locator)
            fresh.append(reference)
        elif locators[position] is None:
            locators[position] = reference.locator
            fresh.append(reference)
        elif locators[position] != reference.locator:
            _raise_parse("search result identity")
    return tuple(seen), tuple(locators), tuple(fresh)


def _parse_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    forms = soup.select("form")
    if not forms:
        _raise_parse("form")
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_parse("weekly form")
    form = forms[0]
    fields = _form_fields(form)
    if not any(field.name == "_csrf" and field.value for field in fields):
        _raise_parse("_csrf")
    action = urljoin(f"{BASE_URL}/", str(form.get("action", "")))
    date_types = form.select('input[name="dateType"]')
    search_types = form.select('input[name="searchType"]')
    submitted_search_types = tuple(
        field.value for field in fields if field.name == "searchType"
    )
    if (
        str(form.get("method", "")).casefold() != "post"
        or action != _WEEKLY_RESULTS_URL
        or len(form.select('select[name="searchCriteria.ward"]')) != 1
        or len(form.select('select[name="week"]')) != 1
        or len(search_types) != 1
        or str(search_types[0].get("value", "")) != "Application"
        or submitted_search_types != ("Application",)
        or len(date_types) != len(_DATE_TYPES)
        or any(
            str(control.get("type", "")).casefold() != "radio" for control in date_types
        )
        or tuple(str(control.get("value", "")) for control in date_types) != _DATE_TYPES
    ):
        _raise_parse("weekly form")
    return form


def _parse_advanced_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    forms = soup.select("form#advancedSearchForm")
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_parse("advanced form")
    form = forms[0]
    action = urljoin(f"{BASE_URL}/", str(form.get("action", "")))
    if (
        str(form.get("method", "")).casefold() != "post"
        or action != _ADVANCED_RESULTS_URL
    ):
        _raise_parse("advanced form")
    status_selects: dict[str, Tag] = {}
    for name in (
        "searchCriteria.caseStatus",
        "searchCriteria.appealStatus",
    ):
        controls = form.select(f'select[name="{name}"]')
        if len(controls) != 1:
            _raise_parse("advanced form status fields")
        status_selects[name] = controls[0]
    for query in _STATUS_QUERIES:
        options = tuple(
            str(option.get("value", ""))
            for option in status_selects[query.field].select("option[value]")
        )
        if options.count(query.value) != 1:
            _raise_parse("advanced form status options")
    field_names = {field.name for field in _form_fields(form)}
    if not {
        "_csrf",
        "searchType",
        "date(applicationReceivedStart)",
        "date(applicationReceivedEnd)",
    }.issubset(field_names):
        _raise_parse("advanced form")
    if any(
        field.name != "_csrf" and field.value
        for field in _form_fields(form)
    ):
        _raise_parse("advanced form neutral filters")
    return form


def _form_fields(form: Tag) -> tuple[FormField, ...]:
    fields: list[FormField] = []
    for control in form.select("input[name], select[name], textarea[name]"):
        name = control.get("name")
        if not isinstance(name, str):
            continue
        if control.name == "input":
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "image", "reset", "submit"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            value = str(control.get("value", ""))
        elif control.name == "select":
            selected = control.select_one("option[selected]") or control.select_one(
                "option"
            )
            value = "" if selected is None else str(selected.get("value", ""))
        else:
            value = control.get_text(strip=True)
        fields.append(FormField(name=name, value=value))
    return tuple(fields)


def _override_fields(form: Tag, values: dict[str, str]) -> tuple[FormField, ...]:
    fields = []
    replaced: set[str] = set()
    for field in _form_fields(form):
        if field.name in values:
            fields.append(FormField(name=field.name, value=values[field.name]))
            replaced.add(field.name)
        else:
            fields.append(field)
    fields.extend(
        FormField(name=name, value=value)
        for name, value in values.items()
        if name not in replaced
    )
    return tuple(fields)


def _weekly_queries(
    form: Tag,
    scope: BarnetDiscoveryScope,
) -> tuple[_WeeklyQuery, ...]:
    offered: dict[date, str] = {}
    for option in form.select('select[name="week"] option[value]'):
        value = str(option.get("value", ""))
        parsed = _parse_date(value)
        if parsed is None or parsed.weekday() != 0:
            continue
        if parsed in offered:
            _raise_parse("weekly form weeks")
        offered[parsed] = value
    expected = _intersecting_mondays(scope)
    if any(monday not in offered for monday in expected):
        _raise_parse("weekly form weeks")
    return tuple(
        _WeeklyQuery(
            week_start=monday,
            portal_week=offered[monday],
            date_type=date_type,
        )
        for monday in expected
        for date_type in _DATE_TYPES
    )


def _weekly_request(
    form: Tag,
    query: _WeeklyQuery,
    page: int,
) -> PortalRequest:
    if page == 1:
        return PortalRequest(
            url=HttpUrl(_WEEKLY_RESULTS_URL),
            intent=RequestIntent.SEARCH,
            method=RequestMethod.POST,
            form=_override_fields(
                form,
                {
                    "searchCriteria.ward": "",
                    "week": query.portal_week,
                    "dateType": query.date_type,
                },
            ),
        )
    return PortalRequest(
        url=HttpUrl(f"{_PAGED_RESULTS_URL}?action=page&searchCriteria.page={page}"),
        intent=RequestIntent.SEARCH,
    )


def _advanced_request(
    form: Tag,
    query: _AdvancedQuery,
    page: int,
) -> PortalRequest:
    if page == 1:
        values = {
            "searchCriteria.caseStatus": "",
            "searchCriteria.appealStatus": "",
            "date(applicationReceivedStart)": "",
            "date(applicationReceivedEnd)": "",
        }
        if isinstance(query, _ReceivedDateQuery):
            values.update(
                {
                    "date(applicationReceivedStart)": query.start.strftime("%d/%m/%Y"),
                    "date(applicationReceivedEnd)": query.end.strftime("%d/%m/%Y"),
                }
            )
        else:
            values[query.field] = query.value
        return PortalRequest(
            url=HttpUrl(_ADVANCED_RESULTS_URL),
            intent=RequestIntent.SEARCH,
            method=RequestMethod.POST,
            form=_override_fields(form, values),
        )
    return PortalRequest(
        url=HttpUrl(f"{_PAGED_RESULTS_URL}?action=page&searchCriteria.page={page}"),
        intent=RequestIntent.SEARCH,
    )


def _parse_search_page(body: bytes) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
    return _parse_result_list(soup, terminal_first_page_marker="1")


def _parse_advanced_search_page(body: bytes, *, page: int) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
    detail_tables = soup.select("#simpleDetailsTable")
    if detail_tables:
        if page != 1:
            _raise_parse("advanced detail redirect")
        return _parse_redirected_detail(body, soup, detail_tables)
    return _parse_result_list(
        soup,
        terminal_first_page_marker="" if page == 1 else None,
    )


def _parse_result_list(
    soup: BeautifulSoup,
    *,
    terminal_first_page_marker: str | None,
) -> _SearchPage:
    references = []
    for row in soup.select("li.searchresult"):
        link = row.select_one('a[href*="applicationDetails.do"]')
        if not isinstance(link, Tag):
            _raise_parse("search result summary link")
        href = str(link.get("href", ""))
        locators = parse_qs(urlsplit(href).query).get("keyVal", [])
        if len(locators) != 1 or not locators[0]:
            _raise_parse("search result keyVal")
        reference = _labelled_value_any(row, "reference", "ref. no", "ref no")
        references.append(
            SourceReference(
                source_id=CURRENT_SOURCE,
                reference=reference,
                locator=locators[0],
            )
        )
    try:
        reported = _reported_count(
            soup,
            row_count=len(references),
        )
    except BarnetParseError:
        if not _is_uncounted_terminal_first_page(
            soup,
            row_count=len(references),
            expected_page_marker=terminal_first_page_marker,
        ):
            raise
        reported = len(references)
    showing_ranges = _showing_ranges(soup, row_count=len(references))
    if not showing_ranges and soup.select_one('a[href*="pagedSearchResults.do"]'):
        _raise_parse("reported result count")
    displayed_range = None if not showing_ranges else showing_ranges[0][:2]
    if showing_ranges:
        showing_range = showing_ranges[0]
        if (
            any(value != showing_range for value in showing_ranges[1:])
            or showing_range[2] != reported
        ):
            _raise_parse("reported result count")
        _validate_showing_pager(
            soup,
            showing_range,
            allow_empty_first_page_marker=terminal_first_page_marker == "",
        )
    return _SearchPage(
        references=tuple(references),
        reported=reported,
        displayed_range=displayed_range,
    )


def _parse_redirected_detail(
    body: bytes,
    soup: BeautifulSoup,
    detail_tables: list[Tag],
) -> _SearchPage:
    if (
        len(detail_tables) != 1
        or soup.select_one("li.searchresult") is not None
        or "no results found" in soup.get_text(" ", strip=True).casefold()
    ):
        _raise_parse("advanced detail redirect")
    locators = []
    for link in soup.select('a[href*="applicationDetails.do"]'):
        values = parse_qs(urlsplit(str(link.get("href", ""))).query).get(
            "keyVal",
            [],
        )
        if len(values) != 1 or not values[0]:
            _raise_parse("advanced detail keyVal")
        locators.append(values[0])
    unique_locators = set(locators)
    if len(unique_locators) != 1:
        _raise_parse("advanced detail keyVal")
    fields = _parse_summary(body)
    return _SearchPage(
        references=(
            SourceReference(
                source_id=CURRENT_SOURCE,
                reference=_required_field(
                    fields,
                    "reference",
                    "application reference",
                ),
                locator=unique_locators.pop(),
            ),
        ),
        reported=1,
    )


def _reported_count(
    soup: BeautifulSoup,
    *,
    row_count: int,
) -> int:
    element = soup.select_one("[data-result-count]")
    if isinstance(element, Tag):
        return int(str(element.get("data-result-count")))
    text = soup.get_text(" ", strip=True)
    if "no results found" in text.casefold():
        return 0
    showing_ranges = _showing_ranges(soup, row_count=row_count)
    if showing_ranges:
        return showing_ranges[0][2]
    match = re.search(
        r"(?:showing\s+\d+\s*[-\N{EN DASH}]\s*\d+\s+of|"
        r"displaying.*?of|total)\s+(\d+)(?:\s+results?)?",
        text,
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse("reported result count")
    return int(match.group(1))


def _validate_showing_pager(
    soup: BeautifulSoup,
    displayed_range: tuple[int, int, int],
    *,
    allow_empty_first_page_marker: bool,
) -> None:
    visible_page = _visible_result_page(soup, displayed_range)
    current_pages = (
        (visible_page,)
        if visible_page is not None
        else _current_result_pages(
            soup,
            allow_empty_first_page_marker=allow_empty_first_page_marker,
        )
    )
    numbered_pages = _numbered_result_pages(soup)
    if (
        displayed_range[1] == displayed_range[2]
        and numbered_pages
        and (
            len(current_pages) != 1
            or any(page > current_pages[0] for page in numbered_pages)
        )
    ):
        _raise_parse("reported result count")


def _numbered_result_pages(soup: BeautifulSoup) -> tuple[int, ...]:
    pages = []
    for link in soup.select('a[href*="pagedSearchResults.do"]'):
        query = parse_qs(urlsplit(str(link.get("href", ""))).query)
        actions = query.get("action", [])
        values = query.get("searchCriteria.page", [])
        if actions != ["page"] or len(values) != 1 or not values[0].isdigit():
            _raise_parse("reported result count")
        page = int(values[0])
        if page < 1:
            _raise_parse("reported result count")
        pages.append(page)
    return tuple(pages)


def _showing_ranges(
    soup: BeautifulSoup,
    *,
    row_count: int,
) -> tuple[tuple[int, int, int], ...]:
    showing_ranges = []
    for marker in soup.select(".showing"):
        match = re.fullmatch(
            r"showing\s+(\d+)\s*[-\N{EN DASH}]\s*(\d+)\s+of\s+"
            r"(\d+)(?:\s+results?)?",
            marker.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if match is None:
            _raise_parse("reported result count")
        first, last, total = (int(match.group(index)) for index in range(1, 4))
        if not 1 <= first <= last <= total or last - first + 1 != row_count:
            _raise_parse("reported result count")
        showing_ranges.append((first, last, total))
    return tuple(showing_ranges)


def _visible_result_page(
    soup: BeautifulSoup,
    displayed_range: tuple[int, int, int],
) -> int | None:
    labels = tuple(
        marker.get_text(" ", strip=True)
        for pager in soup.select(".pager")
        for marker in pager.find_all("strong", recursive=False)
    )
    if not labels:
        return None
    pages = tuple(
        int(match.group(0))
        for label in labels
        if (match := re.fullmatch(r"[1-9]\d*", label)) is not None
    )
    if len(pages) != len(labels) or any(page != pages[0] for page in pages[1:]):
        return _raise_parse("reported result count")
    selected_capacities = soup.select(
        'select[name="searchCriteria.resultsPerPage"] option[selected]'
    )
    try:
        (selected_capacity,) = selected_capacities
        capacity = int(str(selected_capacity.get("value", "")).strip())
    except ValueError:
        return _raise_parse("reported result count")
    page = pages[0]
    first, last, total = displayed_range
    expected_first = (page - 1) * capacity + 1
    expected_last = min(expected_first + capacity - 1, total)
    if capacity <= 0 or (first, last) != (expected_first, expected_last):
        return _raise_parse("reported result count")
    return page


def _current_result_pages(
    soup: BeautifulSoup,
    *,
    allow_empty_first_page_marker: bool,
) -> tuple[int, ...]:
    page_values = tuple(
        str(control.get("value", "")).strip()
        for control in soup.select('input[name="searchCriteria.page"][value]')
    )
    if allow_empty_first_page_marker and page_values == ("",):
        return (1,)
    try:
        return tuple(int(value) for value in page_values)
    except ValueError:
        return _raise_parse("reported result count")


def _is_uncounted_terminal_first_page(
    soup: BeautifulSoup,
    *,
    row_count: int,
    expected_page_marker: str | None = "1",
) -> bool:
    page_inputs = soup.select('input[name="searchCriteria.page"][value]')
    if (
        expected_page_marker is None
        or row_count == 0
        or len(page_inputs) != 1
        or str(page_inputs[0].get("value", "")).strip() != expected_page_marker
        or soup.select_one(".showing") is not None
    ):
        return False
    selected_capacities = soup.select(
        'select[name="searchCriteria.resultsPerPage"] option[selected]'
    )
    if len(selected_capacities) != 1:
        return False
    try:
        capacity = int(str(selected_capacities[0].get("value", "")).strip())
    except ValueError:
        return False
    if capacity <= 0 or row_count >= capacity:
        return False
    queries = tuple(
        parse_qs(urlsplit(str(link.get("href", ""))).query)
        for link in soup.select('a[href*="pagedSearchResults.do"]')
    )
    return not any(
        query.get("searchCriteria.page") or "page" in query.get("action", [])
        for query in queries
    )


def _detail_request(
    locator: str,
    active_tab: str,
    intent: RequestIntent,
) -> PortalRequest:
    url = (
        f"{BASE_URL}/applicationDetails.do?keyVal={quote(locator, safe='')}"
        f"&activeTab={quote(active_tab, safe='')}"
    )
    return PortalRequest(url=HttpUrl(url), intent=intent)


def _parse_summary(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("#simpleDetailsTable")
    if not isinstance(table, Tag):
        _raise_parse("#simpleDetailsTable")
    fields: dict[str, str] = {}
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < _MINIMUM_LABELLED_CELLS:
            continue
        label = _normalise_label(cells[0].get_text(" ", strip=True))
        fields[label] = cells[-1].get_text(" ", strip=True)
    if not fields:
        _raise_parse("summary labelled values")
    return fields


async def _fetch_documents(
    session: PortalSession,
    locator: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[BarnetDocumentV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            _detail_request(locator, "documents", RequestIntent.DETAIL)
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        return _parse_documents(capture.body)
    except BarnetParseError as error:
        return (), FailedSection(code=error.code)


def _parse_documents(
    body: bytes,
) -> tuple[tuple[BarnetDocumentV1, ...], SectionState]:
    soup = BeautifulSoup(body, "html.parser")
    unavailable = _unavailable_state(soup, "documents")
    if unavailable is not None:
        return (), unavailable
    expected = _section_count(soup, ("documents",))
    table = soup.select_one('table[summary="Documents" i]')
    if not isinstance(table, Tag):
        if expected == 0:
            return (), EmptySection()
        _raise_parse("documents table")
    documents = []
    for values, row in _table_rows(table):
        link = row.select_one("a[href]")
        if not isinstance(link, Tag):
            _raise_parse("document metadata link")
        description = _mapping_value(values, "description")
        document_type = _mapping_value(values, "document type", "type")
        title = description or document_type or link.get_text(" ", strip=True)
        documents.append(
            BarnetDocumentV1(
                title=title,
                url=HttpUrl(urljoin(f"{BASE_URL}/", str(link.get("href", "")))),
                published_date=_parse_date(
                    _mapping_value(values, "date published", "date")
                ),
                document_type=document_type,
                drawing_number=_mapping_value(
                    values,
                    "drawing number",
                    "drawing no",
                ),
                description=description,
            )
        )
    _assert_count("documents", expected, len(documents))
    return tuple(documents), collection_state(len(documents))


async def _fetch_comments(
    session: PortalSession,
    locator: str,
    tab: _CommentTab,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[BarnetCommentV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            _detail_request(locator, tab.active_tab, RequestIntent.COMMENTS)
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        return _parse_comments(capture.body, tab.category, tab.count_labels)
    except BarnetParseError as error:
        return (), FailedSection(code=error.code)


def _parse_comments(
    body: bytes,
    category: str,
    count_labels: tuple[str, ...],
) -> tuple[tuple[BarnetCommentV1, ...], SectionState]:
    soup = BeautifulSoup(body, "html.parser")
    unavailable = _unavailable_state(soup, category)
    if unavailable is not None:
        return (), unavailable
    expected = _section_count(soup, count_labels)
    tables = [
        table
        for table in soup.select("table[summary]")
        if isinstance(table, Tag)
        and any(
            label in str(table.get("summary", "")).casefold() for label in count_labels
        )
    ]
    comments = []
    if tables:
        rows = ((values, row) for table in tables for values, row in _table_rows(table))
        for index, (values, row) in enumerate(rows, start=1):
            text = _mapping_value(
                values,
                "comment",
                "comments",
                "representation",
                "response",
                "summary",
            )
            if text is None:
                _raise_parse(f"{category} comment text")
            source_id = row.get("data-comment-id")
            comment_id = (
                str(source_id)
                if isinstance(source_id, str) and source_id
                else str(index)
            )
            comments.append(
                BarnetCommentV1(
                    comment_id=f"{category}-{comment_id}",
                    text=text,
                    category=category,
                )
            )
    else:
        cards = soup.select("#comments > .comment")
        if not cards:
            if expected == 0:
                return (), EmptySection()
            _raise_parse(f"{category} comments table")
        if category == "consultee" and not any(
            card.select_one(".comment-text") for card in cards
        ):
            if expected == 0:
                return (), EmptySection()
            return (), UnavailableSection(
                reason="consultee response text is not exposed by the portal"
            )
        for index, card in enumerate(cards, start=1):
            text = _comment_card_text(card, category)
            source_id = card.get("data-comment-id")
            comment_id = (
                str(source_id)
                if isinstance(source_id, str) and source_id
                else str(index)
            )
            comments.append(
                BarnetCommentV1(
                    comment_id=f"{category}-{comment_id}",
                    text=text,
                    category=category,
                )
            )
    _assert_count(f"{category} comments", expected, len(comments))
    return tuple(comments), collection_state(len(comments))


def _comment_card_text(card: Tag, category: str) -> str:
    content = card.select_one(".comment-text")
    if isinstance(content, Tag):
        text = content.get_text(" ", strip=True)
        if text:
            return text
    return _raise_parse(f"{category} comment text")


def _combined_comment_state(
    public: SectionState,
    consultee: SectionState,
    count: int,
) -> SectionState:
    states = (public, consultee)
    failed = next((state for state in states if isinstance(state, FailedSection)), None)
    if failed is not None:
        return failed
    if any(isinstance(state, UnavailableSection) for state in states):
        return UnavailableSection(reason="one or more comment tabs are unavailable")
    return collection_state(count)


def _table_rows(table: Tag) -> Iterable[tuple[dict[str, str], Tag]]:
    headers = tuple(
        _normalise_label(header.get_text(" ", strip=True))
        for header in table.select("thead th")
    )
    rows = table.select("tbody tr") or table.select("tr")
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        if headers and len(headers) == len(cells):
            values = {
                header: cell.get_text(" ", strip=True)
                for header, cell in zip(headers, cells, strict=True)
            }
        else:
            values = {
                str(index): cell.get_text(" ", strip=True)
                for index, cell in enumerate(cells)
            }
        yield values, row


def _section_count(soup: BeautifulSoup, labels: tuple[str, ...]) -> int:
    for element in soup.select("[data-section][data-count]"):
        section = str(element.get("data-section", "")).casefold()
        if any(label in section for label in labels):
            return int(str(element.get("data-count")))
    text = soup.get_text(" ", strip=True)
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*\((\d+)\)", text, re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return _raise_parse(f"{'/'.join(labels)} displayed count")


def _unavailable_state(
    soup: BeautifulSoup,
    section: str,
) -> UnavailableSection | None:
    text = soup.get_text(" ", strip=True).casefold()
    if "information is not available" in text or "section is not available" in text:
        return UnavailableSection(reason=f"{section} is not exposed by the portal")
    return None


def _assert_count(section: str, expected: int, actual: int) -> None:
    if expected != actual:
        raise BarnetCountMismatchError(section, expected, actual)


def _labelled_value(container: Tag, label: str) -> str:
    strings = tuple(container.stripped_strings)
    target = label.casefold()
    for index, value in enumerate(strings):
        normalised = value.strip().rstrip(":").casefold()
        if normalised == target and index + 1 < len(strings):
            return strings[index + 1].strip()
        prefix = f"{target}:"
        if value.casefold().startswith(prefix):
            return value[len(prefix) :].strip()
    return _raise_parse(f"labelled {label}")


def _labelled_value_any(container: Tag, *labels: str) -> str:
    for label in labels:
        try:
            return _labelled_value(container, label)
        except BarnetParseError:
            continue
    return _raise_parse(f"labelled {'/'.join(labels)}")


def _normalise_label(value: str) -> str:
    return " ".join(value.strip().rstrip(":").casefold().split())


def _mapping_value(values: dict[str, str], *labels: str) -> str | None:
    for label in labels:
        value = values.get(label)
        if value:
            return value
    return None


def _required_field(fields: dict[str, str], *names: str) -> str:
    value = _optional_field(fields, *names)
    if value is None:
        _raise_parse(f"summary {'/'.join(names)}")
    return value


def _optional_field(fields: dict[str, str], *names: str) -> str | None:
    return _mapping_value(fields, *(_normalise_label(name) for name in names))


def _optional_date(fields: dict[str, str], *names: str) -> date | None:
    value = _optional_field(fields, *names)
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        _raise_parse(f"date {'/'.join(names)}")
    return parsed


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    for date_format in _DATE_FORMATS:
        try:
            return (
                datetime.strptime(value.strip(), date_format).replace(tzinfo=UTC).date()
            )
        except ValueError:
            continue
    return None


def _required(value: str, pattern: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(pattern)
    return match.group(1).strip()


class BarnetParseError(ValueError):
    """A required field was absent from a Barnet response."""

    def __init__(self, field: str) -> None:
        """Name the field that failed without retaining response content."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"required Barnet field did not match {field}")


def _raise_parse(field: str) -> NoReturn:
    raise BarnetParseError(field)


class BarnetCountMismatchError(BarnetParseError):
    """A displayed section or search count disagreed with its rows."""

    def __init__(self, section: str, expected: int, actual: int) -> None:
        """Describe only the bounded count disagreement."""
        super().__init__(f"{section} count expected {expected} actual {actual}")


class BarnetCheckpointError(ValueError):
    """A saved live query no longer exists in the weekly form."""

    def __init__(self, query: str) -> None:
        """Name the invalid query without including form or session data."""
        super().__init__(f"Barnet checkpoint query is unavailable: {query}")


class BarnetRoutingError(ValueError):
    """A live Barnet reference lacks its IDOX keyVal locator."""

    def __init__(self, reference: str) -> None:
        """Name the unroutable human reference."""
        super().__init__(f"Barnet reference has no keyVal locator: {reference}")


class BarnetReferenceMismatchError(ValueError):
    """The routed IDOX record published a different human reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Describe the stable-reference mismatch."""
        super().__init__(
            f"Barnet reference mismatch: expected {expected}, got {actual}"
        )
