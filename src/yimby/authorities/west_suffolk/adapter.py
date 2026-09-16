# Copyright (c) 2026 Kostas Stathoulopoulos

"""West Suffolk-owned fixture and live IDOX Public Access adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import Field, HttpUrl

from yimby.domain import (
    ApplicationMetadata,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryEvidenceCapture,
    DiscoveryWindow,
    DocumentRecord,
    EmptySection,
    EvidenceDigest,
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

SOURCE = SourceId("west-suffolk-idox-public-access")
BASE_URL = "https://planning.westsuffolk.gov.uk/online-applications"
_WEEKLY_FORM_URL = f"{BASE_URL}/search.do?action=weeklyList"
_WEEKLY_RESULTS_URL = f"{BASE_URL}/weeklyListResults.do?action=firstPage"
_ADVANCED_FORM_URL = f"{BASE_URL}/search.do?action=advanced"
_ADVANCED_RESULTS_URL = f"{BASE_URL}/advancedSearchResults.do?action=firstPage"
_PAGED_RESULTS_URL = f"{BASE_URL}/pagedSearchResults.do"
_DATE_TYPES = ("DC_Validated", "DC_Decided")
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_MINIMUM_LABELLED_CELLS = 2


class WestSuffolkDiscoveryScope(FrozenModel):
    """Exact live discovery request owning resumable West Suffolk progress."""

    start: date
    end: date
    include_open: bool


class WestSuffolkDiscoveryProofV1(FrozenModel):
    """One retained search page and its safe logical request subject."""

    query_key: str
    page: int = Field(ge=1)
    digest: EvidenceDigest
    response_url: HttpUrl
    request_url: HttpUrl
    request_method: Literal["GET", "POST"]
    request_form: tuple[tuple[str, str], ...] = ()


class WestSuffolkCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable West Suffolk discovery progress."""

    result_page: str
    live_scope: WestSuffolkDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    query_row_count: int = 0
    seen_references: tuple[str, ...] = ()
    discovery_proofs: tuple[WestSuffolkDiscoveryProofV1, ...] = ()
    live_complete: bool = False


class _WeeklyQuery(FrozenModel):
    week: str
    date_type: str

    @property
    def key(self) -> str:
        return f"{self.week}|{self.date_type}"


class _AdvancedQuery(FrozenModel):
    field: Literal[
        "searchCriteria.caseStatus",
        "searchCriteria.appealStatus",
    ]
    value: str

    @property
    def key(self) -> str:
        return f"advanced|{self.field}|{self.value}"


_ADVANCED_QUERIES = (
    _AdvancedQuery(
        field="searchCriteria.caseStatus",
        value="Pending Consideration",
    ),
    _AdvancedQuery(
        field="searchCriteria.caseStatus",
        value="Pending Decision",
    ),
    _AdvancedQuery(
        field="searchCriteria.caseStatus",
        value="Received Awaiting Registration",
    ),
    _AdvancedQuery(
        field="searchCriteria.caseStatus",
        value="Pending Appeal Decision",
    ),
    _AdvancedQuery(
        field="searchCriteria.appealStatus",
        value="Appeal lodged",
    ),
    _AdvancedQuery(
        field="searchCriteria.appealStatus",
        value="Appeal Remitted to Secretary of State ",
    ),
    _AdvancedQuery(
        field="searchCriteria.appealStatus",
        value="High Court Appeal Lodged",
    ),
    _AdvancedQuery(
        field="searchCriteria.appealStatus",
        value="Pending Appeal Decision",
    ),
)


class WestSuffolkDocumentV1(FrozenModel):
    """WestSuffolk document metadata without attachment bodies."""

    title: str
    url: HttpUrl
    published_date: date | None = None
    document_type: str | None = None
    drawing_number: str | None = None
    description: str | None = None
    source_links: tuple[HttpUrl, ...] = ()


class WestSuffolkApplicationV1(FrozenModel):
    """WestSuffolk-native IDOX application."""

    idox_key: str
    application_reference: str
    proposal_text: str
    case_status: str
    documents: tuple[WestSuffolkDocumentV1, ...]
    ward_name: str | None = None
    parish_name: str | None = None
    alternative_reference: str | None = None
    address: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision_date: date | None = None
    comment_state: SectionState | None = None


class WestSuffolkAdapter:
    """Own WestSuffolk request, parsing, completeness, and normalisation rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("west-suffolk"),
        name="West Suffolk Council",
        kind=AuthorityKind.DISTRICT,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: WestSuffolkCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[WestSuffolkCheckpointV1]]:
        """Use fixtures or West Suffolk's captured weekly and active IDOX flows."""
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
        checkpoint: WestSuffolkCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[WestSuffolkCheckpointV1]]:
        page = "1" if checkpoint is None else checkpoint.result_page
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
            for value in re.findall(r'data-west-suffolk-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(html, r'data-west-suffolk-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=WestSuffolkCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(  # noqa: C901, PLR0912
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: WestSuffolkCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[WestSuffolkCheckpointV1]]:
        requested_scope = WestSuffolkDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != requested_scope:
            progress = WestSuffolkCheckpointV1(
                result_page="live",
                live_scope=requested_scope,
            )
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_WEEKLY_FORM_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_form(form_capture.body)
        weekly_queries = tuple(
            _WeeklyQuery(week=week, date_type=date_type)
            for week in _intersecting_weeks(form, window)
            for date_type in _DATE_TYPES
        )
        query_keys = tuple(query.key for query in weekly_queries) + tuple(
            query.key for query in _ADVANCED_QUERIES if window.include_open
        )
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            raise WestSuffolkCheckpointError(progress.active_query)
        pending_weekly = tuple(
            query
            for query in weekly_queries
            if query.key not in progress.completed_queries
        )
        for weekly_query in pending_weekly:
            page, row_count = _query_progress(progress, weekly_query.key)
            if progress.active_query == weekly_query.key and page > 1:
                replay_request = _weekly_request(
                    form,
                    weekly_query.week,
                    weekly_query.date_type,
                    1,
                )
                await session.fetch(replay_request)
            while True:
                request = _weekly_request(
                    form,
                    weekly_query.week,
                    weekly_query.date_type,
                    page,
                )
                capture = await session.fetch(request)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=weekly_query.key,
                        page=page,
                        row_count=row_count,
                    ),
                    search_page=_parse_search_page(capture.body),
                    all_query_keys=query_keys,
                    proof=_discovery_proof(
                        weekly_query.key,
                        page,
                        request,
                        capture,
                    ),
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                    evidence=(_discovery_evidence(request, capture),),
                    evidence_key=weekly_query.key,
                    evidence_page=page,
                )
                progress = next_checkpoint
                if last_page:
                    break
                row_count = next_checkpoint.query_row_count
                page += 1
        if not window.include_open:
            if pending_weekly:
                return
            completed_checkpoint = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(), next_checkpoint=completed_checkpoint, complete=True
            )
            return

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
        pending_open = tuple(
            query
            for query in _ADVANCED_QUERIES
            if query.key not in progress.completed_queries
        )
        for open_query in pending_open:
            page, row_count = _query_progress(progress, open_query.key)
            if progress.active_query == open_query.key and page > 1:
                await session.fetch(_advanced_request(advanced_form, open_query, 1))
            while True:
                request = _advanced_request(advanced_form, open_query, page)
                capture = await session.fetch(request)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=open_query.key,
                        page=page,
                        row_count=row_count,
                    ),
                    search_page=_parse_advanced_search_page(capture.body, page=page),
                    all_query_keys=query_keys,
                    proof=_discovery_proof(
                        open_query.key,
                        page,
                        request,
                        capture,
                    ),
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                    evidence=(_discovery_evidence(request, capture),),
                    evidence_key=open_query.key,
                    evidence_page=page,
                )
                progress = next_checkpoint
                if last_page:
                    break
                row_count = next_checkpoint.query_row_count
                page += 1
        if not pending_open:
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
    ) -> NativeSnapshot[WestSuffolkApplicationV1]:
        """Read fixture detail or WestSuffolk live summary and exposed sections."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[WestSuffolkApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{BASE_URL}/application/{encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        documents = tuple(
            WestSuffolkDocumentV1(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-west-suffolk-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = WestSuffolkApplicationV1(
            idox_key=_required_fixture(
                html, r'data-west-suffolk-key="([^"]+)"', "IDOX key"
            ),
            application_reference=reference.reference,
            proposal_text=unescape(
                _required_fixture(
                    html, r'data-west-suffolk-proposal="([^"]+)"', "proposal"
                )
            ),
            case_status=_required_fixture(
                html, r'data-west-suffolk-status="([^"]+)"', "status"
            ),
            ward_name=_required_fixture(
                html, r'data-west-suffolk-ward="([^"]+)"', "ward"
            ),
            parish_name=_required_fixture(
                html, r'data-west-suffolk-parish="([^"]+)"', "parish"
            ),
            documents=documents,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=UnavailableSection(
                    reason="comment detail was not verified live"
                ),
            ),
            evidence=(detail,),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[WestSuffolkApplicationV1]:
        if reference.locator is None:
            raise WestSuffolkRoutingError(reference.reference)
        summary = await session.fetch(
            _detail_request(reference.locator, "summary", RequestIntent.DETAIL)
        )
        fields = _parse_summary(summary.body)
        published_reference = _required_field(fields, "reference")
        if published_reference != reference.reference:
            raise WestSuffolkReferenceMismatchError(
                reference.reference, published_reference
            )
        evidence: list[EvidenceCapture] = [summary]
        documents, document_state = await _fetch_documents(
            session, reference.locator, evidence
        )
        comment_state = UnavailableSection(
            reason="West Suffolk representations are published only as attachments"
        )
        payload = WestSuffolkApplicationV1(
            idox_key=reference.locator,
            application_reference=published_reference,
            proposal_text=_required_field(fields, "proposal", "description"),
            case_status=_required_field(fields, "status"),
            ward_name=_optional_field(fields, "ward"),
            parish_name=_optional_field(fields, "parish"),
            documents=documents,
            alternative_reference=_optional_field(fields, "alternative reference"),
            address=_optional_field(fields, "address"),
            received_date=_optional_date(fields, "received date"),
            validated_date=_optional_date(fields, "validated date"),
            decision_date=_optional_date(fields, "decision date"),
            comment_state=comment_state,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=document_state,
                comments=comment_state,
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[WestSuffolkApplicationV1],
    ) -> NormalisedObservation:
        """Map WestSuffolk-native fields to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_text,
            status=payload.case_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(
                    title=item.title,
                    url=item.url,
                    category=item.document_type,
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
            normaliser_version="west-suffolk-v3",
            metadata=ApplicationMetadata(
                aliases=(
                    ()
                    if payload.alternative_reference is None
                    else (payload.alternative_reference,)
                ),
                address=payload.address,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                source_url=snapshot.evidence[0].url,
            ),
        )


class _SearchPage(FrozenModel):
    references: tuple[SourceReference, ...]
    reported: int


class _ActivePage(FrozenModel):
    query_key: str
    page: int
    row_count: int


def _query_progress(
    progress: WestSuffolkCheckpointV1,
    query_key: str,
) -> tuple[int, int]:
    if progress.active_query == query_key:
        return progress.next_page, progress.query_row_count
    return 1, 0


def _advance_checkpoint(
    progress: WestSuffolkCheckpointV1,
    *,
    active_page: _ActivePage,
    search_page: _SearchPage,
    all_query_keys: tuple[str, ...],
    proof: WestSuffolkDiscoveryProofV1,
) -> tuple[WestSuffolkCheckpointV1, tuple[SourceReference, ...], bool]:
    next_row_count = active_page.row_count + len(search_page.references)
    if next_row_count > search_page.reported or (
        not search_page.references and next_row_count < search_page.reported
    ):
        raise WestSuffolkCountMismatchError(
            active_page.query_key,
            search_page.reported,
            next_row_count,
        )
    seen = set(progress.seen_references)
    fresh = []
    for reference in search_page.references:
        if reference.reference not in seen:
            seen.add(reference.reference)
            fresh.append(reference)
    if any(
        (retained.query_key, retained.page) == (proof.query_key, proof.page)
        for retained in progress.discovery_proofs
    ):
        raise WestSuffolkCheckpointError(active_page.query_key)
    discovery_proofs = (*progress.discovery_proofs, proof)
    last_page = next_row_count == search_page.reported
    if last_page:
        completed_queries = (*progress.completed_queries, active_page.query_key)
        checkpoint = progress.model_copy(
            update={
                "completed_queries": completed_queries,
                "active_query": None,
                "next_page": 1,
                "query_row_count": 0,
                "seen_references": tuple(seen),
                "discovery_proofs": discovery_proofs,
                "live_complete": len(completed_queries) == len(all_query_keys),
            }
        )
    else:
        checkpoint = progress.model_copy(
            update={
                "active_query": active_page.query_key,
                "next_page": active_page.page + 1,
                "query_row_count": next_row_count,
                "seen_references": tuple(seen),
                "discovery_proofs": discovery_proofs,
            }
        )
    return checkpoint, tuple(fresh), last_page


def _parse_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form")
    if not isinstance(form, Tag):
        _raise_parse("form")
    if not any(field.name == "_csrf" and field.value for field in _form_fields(form)):
        _raise_parse("_csrf")
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
    for query in _ADVANCED_QUERIES:
        select = status_selects[query.field]
        options = tuple(
            str(option.get("value", "")) for option in select.select("option[value]")
        )
        if options.count(query.value) != 1:
            _raise_parse("advanced form status options")
    field_names = {field.name for field in _form_fields(form)}
    if not {"_csrf", "searchType"}.issubset(field_names):
        _raise_parse("advanced form")
    return form


def _form_fields(form: Tag) -> tuple[FormField, ...]:
    fields = []
    for control in form.select("input[name], select[name], textarea[name]"):
        name = control.get("name")
        if not isinstance(name, str):
            continue
        if control.name == "input":
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "image", "reset", "submit"}:
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
    replaced = set()
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


def _intersecting_weeks(form: Tag, window: DiscoveryWindow) -> tuple[str, ...]:
    weeks = []
    for option in form.select('select[name="week"] option[value]'):
        value = str(option.get("value", ""))
        parsed = _parse_date(value)
        if (
            parsed is not None
            and parsed.weekday() == 0
            and parsed <= window.end
            and parsed + timedelta(days=6) >= window.start
        ):
            weeks.append((parsed, value))
    return tuple(value for _, value in sorted(weeks))


def _weekly_request(form: Tag, week: str, date_type: str, page: int) -> PortalRequest:
    if page == 1:
        return PortalRequest(
            url=HttpUrl(_WEEKLY_RESULTS_URL),
            intent=RequestIntent.SEARCH,
            method=RequestMethod.POST,
            form=_override_fields(
                form,
                {
                    "searchCriteria.parish": "",
                    "searchCriteria.ward": "",
                    "week": week,
                    "dateType": date_type,
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
        return PortalRequest(
            url=HttpUrl(_ADVANCED_RESULTS_URL),
            intent=RequestIntent.SEARCH,
            method=RequestMethod.POST,
            form=_override_fields(
                form,
                {
                    "searchCriteria.caseStatus": (
                        query.value
                        if query.field == "searchCriteria.caseStatus"
                        else ""
                    ),
                    "searchCriteria.appealStatus": (
                        query.value
                        if query.field == "searchCriteria.appealStatus"
                        else ""
                    ),
                },
            ),
        )
    return PortalRequest(
        url=HttpUrl(f"{_PAGED_RESULTS_URL}?action=page&searchCriteria.page={page}"),
        intent=RequestIntent.SEARCH,
    )


def _safe_request_form(request: PortalRequest) -> tuple[tuple[str, str], ...]:
    return tuple(
        (field.name, field.value) for field in request.form if field.name != "_csrf"
    )


def _discovery_evidence(
    request: PortalRequest,
    capture: EvidenceCapture,
) -> DiscoveryEvidenceCapture:
    return DiscoveryEvidenceCapture(
        capture=capture,
        request_url=request.url,
        request_method=request.method.value,
        request_form=_safe_request_form(request),
    )


def _discovery_proof(
    query_key: str,
    page: int,
    request: PortalRequest,
    capture: EvidenceCapture,
) -> WestSuffolkDiscoveryProofV1:
    return WestSuffolkDiscoveryProofV1(
        query_key=query_key,
        page=page,
        digest=capture.digest,
        response_url=capture.url,
        request_url=request.url,
        request_method=request.method.value,
        request_form=_safe_request_form(request),
    )


def _parse_search_page(body: bytes) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
    return _parse_result_list(soup, terminal_first_page_markers=("1",))


def _parse_advanced_search_page(body: bytes, *, page: int) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
    detail_tables = soup.select("#simpleDetailsTable")
    if detail_tables:
        if page != 1:
            _raise_parse("advanced detail redirect")
        return _parse_redirected_detail(body, soup, detail_tables)
    return _parse_result_list(
        soup,
        terminal_first_page_markers=("", "1") if page == 1 else (),
    )


def validate_west_suffolk_discovery_proof(
    proof: WestSuffolkDiscoveryProofV1,
    capture: EvidenceCapture,
) -> _SearchPage:
    """Parse one retained page only when its logical request still matches."""
    if capture.digest != proof.digest or capture.url != proof.response_url:
        _raise_parse("discovery evidence identity")
    advanced = proof.query_key.startswith("advanced|")
    if advanced:
        _validate_advanced_proof_request(proof)
        return _parse_advanced_search_page(capture.body, page=proof.page)
    _validate_weekly_proof_request(proof)
    return _parse_search_page(capture.body)


def _validate_weekly_proof_request(proof: WestSuffolkDiscoveryProofV1) -> None:
    week, separator, date_type = proof.query_key.rpartition("|")
    if separator != "|" or date_type not in _DATE_TYPES:
        _raise_parse("discovery weekly query")
    if proof.page == 1:
        values = _request_form_values(proof.request_form)
        if (
            str(proof.request_url) != _WEEKLY_RESULTS_URL
            or proof.request_method != RequestMethod.POST.value
            or values.get("searchCriteria.parish") != ("",)
            or values.get("searchCriteria.ward") != ("",)
            or values.get("week") != (week,)
            or values.get("dateType") != (date_type,)
            or len(values.get("searchType", ())) != 1
        ):
            _raise_parse("discovery weekly request")
        return
    _validate_paged_proof_request(proof)


def _validate_advanced_proof_request(proof: WestSuffolkDiscoveryProofV1) -> None:
    query = next(
        (item for item in _ADVANCED_QUERIES if item.key == proof.query_key),
        None,
    )
    if query is None:
        _raise_parse("discovery advanced query")
    if proof.page == 1:
        values = _request_form_values(proof.request_form)
        other_field = (
            "searchCriteria.appealStatus"
            if query.field == "searchCriteria.caseStatus"
            else "searchCriteria.caseStatus"
        )
        if (
            str(proof.request_url) != _ADVANCED_RESULTS_URL
            or proof.request_method != RequestMethod.POST.value
            or values.get(query.field) != (query.value,)
            or values.get(other_field) != ("",)
        ):
            _raise_parse("discovery advanced request")
        return
    _validate_paged_proof_request(proof)


def _validate_paged_proof_request(proof: WestSuffolkDiscoveryProofV1) -> None:
    expected = f"{_PAGED_RESULTS_URL}?action=page&searchCriteria.page={proof.page}"
    if (
        str(proof.request_url) != expected
        or proof.request_method != RequestMethod.GET.value
        or proof.request_form
    ):
        _raise_parse("discovery page request")


def _request_form_values(
    fields: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for name, value in fields:
        if name == "_csrf":
            _raise_parse("discovery request secret")
        values.setdefault(name, []).append(value)
    return {name: tuple(items) for name, items in values.items()}


def _parse_result_list(
    soup: BeautifulSoup,
    *,
    terminal_first_page_markers: tuple[str, ...],
) -> _SearchPage:
    references = []
    for row in soup.select("li.searchresult"):
        link = row.select_one('a[href*="applicationDetails.do"]')
        if not isinstance(link, Tag):
            _raise_parse("search result summary link")
        locators = parse_qs(urlsplit(str(link.get("href", ""))).query).get("keyVal", [])
        if len(locators) != 1 or not locators[0]:
            _raise_parse("search result keyVal")
        references.append(
            SourceReference(
                source_id=SOURCE,
                reference=_labelled_value_any(row, "reference", "ref. no", "ref no"),
                locator=locators[0],
            )
        )
    try:
        reported = _reported_count(
            soup,
            row_count=len(references),
            allow_empty_first_page_marker="" in terminal_first_page_markers,
        )
    except WestSuffolkParseError:
        if not _is_uncounted_terminal_first_page(
            soup,
            row_count=len(references),
            expected_page_markers=terminal_first_page_markers,
        ):
            raise
        reported = len(references)
    return _SearchPage(references=tuple(references), reported=reported)


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
                source_id=SOURCE,
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
    allow_empty_first_page_marker: bool = False,
) -> int:
    element = soup.select_one("[data-result-count]")
    if isinstance(element, Tag):
        return int(str(element.get("data-result-count")))
    text = soup.get_text(" ", strip=True)
    if "no results found" in text.casefold():
        return 0
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
    if showing_ranges:
        displayed_range = showing_ranges[0]
        if any(value != displayed_range for value in showing_ranges[1:]):
            _raise_parse("reported result count")
        visible_page = _visible_result_page(soup, displayed_range)
        current_pages = (
            (visible_page,)
            if visible_page is not None
            else _current_result_pages(
                soup,
                allow_empty_first_page_marker=allow_empty_first_page_marker,
            )
        )
        numbered_pages = tuple(
            int(value)
            for link in soup.select('a[href*="pagedSearchResults.do"]')
            for value in parse_qs(urlsplit(str(link.get("href", ""))).query).get(
                "searchCriteria.page", ()
            )
        )
        if (
            displayed_range[1] == displayed_range[2]
            and len(current_pages) == 1
            and any(page > current_pages[0] for page in numbered_pages)
        ):
            _raise_parse("reported result count")
        return displayed_range[2]
    match = re.search(
        r"(?:displaying.*?of|total)\s+(\d+)\s+results?",
        text,
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse("reported result count")
    return int(match.group(1))


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
    expected_page_markers: tuple[str, ...] = ("1",),
) -> bool:
    page_inputs = soup.select('input[name="searchCriteria.page"][value]')
    if (
        not expected_page_markers
        or row_count == 0
        or len(page_inputs) != 1
        or str(page_inputs[0].get("value", "")).strip() not in expected_page_markers
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


def _detail_request(locator: str, tab: str, intent: RequestIntent) -> PortalRequest:
    return PortalRequest(
        url=HttpUrl(
            f"{BASE_URL}/applicationDetails.do?keyVal={quote(locator, safe='')}"
            f"&activeTab={quote(tab, safe='')}"
        ),
        intent=intent,
    )


def _parse_summary(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("#simpleDetailsTable")
    if not isinstance(table, Tag):
        _raise_parse("#simpleDetailsTable")
    fields = {}
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= _MINIMUM_LABELLED_CELLS:
            fields[_normalise_label(cells[0].get_text(" ", strip=True))] = cells[
                -1
            ].get_text(" ", strip=True)
    if not fields:
        _raise_parse("summary labelled values")
    return fields


async def _fetch_documents(
    session: PortalSession,
    locator: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[WestSuffolkDocumentV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            _detail_request(locator, "documents", RequestIntent.DETAIL)
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        return _parse_documents(capture.body)
    except WestSuffolkParseError as error:
        return (), FailedSection(code=error.code)


def _parse_documents(
    body: bytes,
) -> tuple[tuple[WestSuffolkDocumentV1, ...], SectionState]:
    soup = BeautifulSoup(body, "html.parser")
    if _is_unavailable(soup):
        return (), UnavailableSection(reason="documents are not exposed")
    expected = _section_count(soup, "documents")
    table = soup.select_one('table[summary="Documents" i]')
    if not isinstance(table, Tag):
        if expected == 0:
            return (), EmptySection()
        _raise_parse("documents table")
    documents = []
    for values, row in _table_rows(table):
        links = tuple(
            HttpUrl(urljoin(f"{BASE_URL}/", str(link.get("href", ""))))
            for link in row.select("a[href]")
        )
        if not links:
            _raise_parse("document metadata link")
        description = _mapping_value(values, "description")
        document_type = _mapping_value(values, "document type", "type")
        documents.append(
            WestSuffolkDocumentV1(
                title=description or document_type or "Document",
                url=links[-1],
                published_date=_parse_date(
                    _mapping_value(values, "date published", "date")
                ),
                document_type=document_type,
                drawing_number=_mapping_value(values, "drawing number", "drawing no"),
                description=description,
                source_links=links,
            )
        )
    _assert_count("documents", expected, len(documents))
    return tuple(documents), collection_state(len(documents))


def _table_rows(table: Tag) -> Iterable[tuple[dict[str, str], Tag]]:
    headers = tuple(
        _normalise_label(header.get_text(" ", strip=True))
        for header in table.select("thead th")
    )
    for row in table.select("tbody tr") or table.select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        values = (
            {
                header: cell.get_text(" ", strip=True)
                for header, cell in zip(headers, cells, strict=True)
            }
            if headers and len(headers) == len(cells)
            else {
                str(index): cell.get_text(" ", strip=True)
                for index, cell in enumerate(cells)
            }
        )
        yield values, row


def _section_count(soup: BeautifulSoup, label: str) -> int:
    for element in soup.select("[data-section][data-count]"):
        if label in str(element.get("data-section", "")).casefold():
            return int(str(element.get("data-count")))
    match = re.search(
        rf"{re.escape(label)}\s*\((\d+)\)",
        soup.get_text(" ", strip=True),
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse(f"{label} displayed count")
    return int(match.group(1))


def _is_unavailable(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True).casefold()
    return "information is not available" in text or "section is not available" in text


def _assert_count(section: str, expected: int, actual: int) -> None:
    if expected != actual:
        raise WestSuffolkCountMismatchError(section, expected, actual)


def _labelled_value(container: Tag, label: str) -> str:
    target = label.casefold()
    strings = tuple(container.stripped_strings)
    for index, value in enumerate(strings):
        normalised = value.strip().rstrip(":").casefold()
        if normalised == target and index + 1 < len(strings):
            return strings[index + 1].strip()
        if value.casefold().startswith(f"{target}:"):
            return value[len(target) + 1 :].strip()
    return _raise_parse(f"labelled {label}")


def _labelled_value_any(container: Tag, *labels: str) -> str:
    for label in labels:
        try:
            return _labelled_value(container, label)
        except WestSuffolkParseError:
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


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class WestSuffolkParseError(ValueError):
    """A required WestSuffolk boundary value was absent or inconsistent."""

    def __init__(self, field: str) -> None:
        """Name the safe boundary field without retaining response content."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing WestSuffolk field {field}")


def _raise_parse(field: str) -> NoReturn:
    raise WestSuffolkParseError(field)


class WestSuffolkCountMismatchError(WestSuffolkParseError):
    """A WestSuffolk displayed count disagreed with parsed records."""

    def __init__(self, section: str, expected: int, actual: int) -> None:
        """Describe the bounded count disagreement."""
        super().__init__(f"{section} count expected {expected} actual {actual}")


class WestSuffolkCheckpointError(ValueError):
    """A saved West Suffolk query is absent from the current inventory."""

    def __init__(self, query: str) -> None:
        """Name the invalid query without exposing session values."""
        super().__init__(f"West Suffolk checkpoint query is unavailable: {query}")


class WestSuffolkRoutingError(ValueError):
    """A WestSuffolk live reference lacks its source-local keyVal."""

    def __init__(self, reference: str) -> None:
        """Name the unroutable human reference."""
        super().__init__(f"West Suffolk reference has no keyVal locator: {reference}")


class WestSuffolkReferenceMismatchError(ValueError):
    """The routed WestSuffolk record published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Describe the stable-reference mismatch."""
        super().__init__(
            f"West Suffolk reference mismatch: expected {expected}, got {actual}"
        )
