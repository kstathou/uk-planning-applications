# Copyright (c) 2026 Kostas Stathoulopoulos

"""Peak District-owned legacy and AssureLive adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl

from yimby.domain import (
    ApplicationMetadata,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    FailedSection,
    FrozenModel,
    NativeDocument,
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
    from collections.abc import AsyncIterator

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalSession

LEGACY_SOURCE = SourceId("peak-district-legacy")
ASSURE_SOURCE = SourceId("peak-district-assurelive")
LEGACY_BASE = "https://portal.peakdistrict.gov.uk"
ASSURE_BASE = "https://planning.peakdistrict.gov.uk/AssureLive"
_ONLINE_BASE = f"{ASSURE_BASE}/ES/Presentation/Planning/OnlinePlanning"
_SEARCH_URL = f"{_ONLINE_BASE}/OnlinePlanningSearch"
_ADVANCED_FORM_URL = f"{_ONLINE_BASE}/OnlinePlanningAdvanceSearchView?SearchFor=0"
_RESULTS_URL = f"{_ONLINE_BASE}/OnlinePlanningSearchResults"
_PAGINATION_URL = f"{_ONLINE_BASE}/SearchResultsForPagination"
_DOCUMENTS_URL = f"{_ONLINE_BASE}/GetOnlineDocuments"
_DOCUMENT_PATH_PREFIX = (
    "/AssureLive/ES/Presentation/Planning/OnlineDisplayDocument/DisplaySearchDocument/"
)
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")
_MINIMUM_LABELLED_CELLS = 2
_DOCUMENT_COLUMN_COUNT = 4
type _DateQueryField = Literal["Received", "Validated", "Decided"]
_DATE_QUERY_FIELDS: tuple[_DateQueryField, ...] = (
    "Received",
    "Validated",
    "Decided",
)
_OPEN_STATUSES = ("REGISTERED", "APPEAL LODGED")
_STATUS_FIELD: Literal["AdvanceSearch.SelectedApplicationStatus"] = (
    "AdvanceSearch.SelectedApplicationStatus"
)
_APPLICATION_NUMBER_PATTERN = re.compile(
    r"Application\s+No\s*:\s*(.*?)\s*\|\s*"
    r"(?:Registered|Received|Validated|Decided)\s*:",
    re.IGNORECASE,
)
_TOTAL_PATTERN = re.compile(r"Total\s+record\(s\)\s*:\s*(\d+)", re.IGNORECASE)
_PAGE_PATTERN = re.compile(r"PagingClick\(['\"]?(\d+)['\"]?\)")


class PeakDistrictDiscoveryScope(FrozenModel):
    """Exact AssureLive request owning resumable discovery progress."""

    start: date
    end: date
    include_open: bool


class PeakDistrictQueryV1(FrozenModel):
    """One official AssureLive query required by the discovery scope."""

    kind: Literal["bounded-date", "older-open"]
    field: Literal[
        "Received",
        "Validated",
        "Decided",
        "AdvanceSearch.SelectedApplicationStatus",
    ]
    value: str

    @property
    def key(self) -> str:
        """Encode a stable checkpoint key without losing portal spelling."""
        return f"{self.kind}|{self.field}|{self.value}"


class PeakDistrictCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable AssureLive discovery progress."""

    row_offset: str = "0"
    window_start: date | None = None
    window_end: date | None = None
    live_scope: PeakDistrictDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page_index: int = 0
    query_row_count: int = 0
    seen_references: tuple[str, ...] = ()
    live_complete: bool = False


class _SearchPage(FrozenModel):
    references: tuple[SourceReference, ...]
    reported: int
    page_index: int
    page_size: int
    form: tuple[FormField, ...]


class _ActivePage(FrozenModel):
    query_key: str
    page_index: int
    row_count: int


class PeakDistrictDocumentV1(NativeDocument):
    """AssureLive document metadata without attachment content."""

    published_date: date | None = None
    document_type: str | None = None


class PeakDistrictApplicationV1(FrozenModel):
    """Peak District-native application and document metadata."""

    park_reference: str
    record_type: str
    proposal_summary: str
    case_status: str
    parish: str
    legacy_record_url: HttpUrl
    development_address: str | None = None
    planning_portal_reference: str | None = None
    validated_date: date | None = None
    assurelive_url: HttpUrl | None = None
    loading_sections: tuple[str, ...] = ()
    applicant_name: str | None = None
    agent_name: str | None = None
    officer_name: str | None = None
    documents: tuple[PeakDistrictDocumentV1, ...] = ()


class _AssureDetail(FrozenModel):
    reference: str
    record_type: str
    proposal: str
    status: str
    parish: str
    address: str | None = None
    registered_date: date | None = None
    applicant_name: str | None = None
    agent_name: str | None = None
    officer_name: str | None = None
    documents_endpoint: str | None = None
    comments_tab: bool = False


class _DocumentPage(FrozenModel):
    documents: tuple[PeakDistrictDocumentV1, ...]
    reported: int
    page_index: int
    page_size: int


class PeakDistrictAdapter:
    """Own the legacy weekly and visible-loading-state boundaries."""

    manifest = AuthorityManifest(
        id=AuthorityId("peak-district"),
        name="Peak District National Park Authority",
        kind=AuthorityKind.NATIONAL_PARK,
        sources=(
            SourceDefinition(
                id=LEGACY_SOURCE,
                base_url=HttpUrl(f"{LEGACY_BASE}/"),
            ),
            SourceDefinition(
                id=ASSURE_SOURCE,
                base_url=HttpUrl(f"{ASSURE_BASE}/"),
                valid_from=date(2025, 1, 1),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        """Use fixtures or enumerate every row in the legacy weekly table."""
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
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        offset = "0" if checkpoint is None else checkpoint.row_offset
        url = (
            f"{LEGACY_BASE}/search?validatedFrom={window.start.isoformat()}"
            f"&validatedTo={window.end.isoformat()}&offset={quote(offset)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=LEGACY_SOURCE, reference=value)
            for value in re.findall(r'data-peak-reference="([^"]+)"', html)
        )
        next_offset = _required_fixture(html, r'data-peak-offset="([^"]+)"', "offset")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=PeakDistrictCheckpointV1(row_offset=next_offset),
            complete=next_offset == "complete",
        )

    async def _discover_live(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        requested_scope = PeakDistrictDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != requested_scope:
            progress = PeakDistrictCheckpointV1(
                row_offset="live",
                window_start=window.start,
                window_end=window.end,
                live_scope=requested_scope,
            )
        queries = peak_district_query_inventory(window)
        query_keys = tuple(query.key for query in queries)
        _assert_checkpoint(progress, query_keys)
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_SEARCH_URL), intent=RequestIntent.SEARCH)
        )
        advanced_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_ADVANCED_FORM_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_search_form(form_capture.body, advanced_capture.body)
        for query in queries:
            if query.key in progress.completed_queries:
                continue
            page_index = (
                progress.next_page_index if progress.active_query == query.key else 0
            )
            row_count = (
                progress.query_row_count if progress.active_query == query.key else 0
            )
            query_request = _query_request(form, query)
            page_form: tuple[FormField, ...] | None = None
            if page_index > 0:
                first_capture = await session.fetch(query_request)
                page_form = _parse_search_page(
                    first_capture.body,
                    expected_page=0,
                ).form
            while True:
                capture = await session.fetch(
                    query_request
                    if page_index == 0
                    else _pagination_request(
                        query_request.form,
                        page_form,
                        page_index,
                    )
                )
                search_page = _parse_search_page(
                    capture.body,
                    expected_page=page_index,
                )
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=query.key,
                        page_index=page_index,
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
                page_index = next_checkpoint.next_page_index
                row_count = next_checkpoint.query_row_count
                page_form = search_page.form

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        """Read fixture summary or the captured legacy result page."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{ASSURE_BASE}/Planning/Details/{encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        payload = PeakDistrictApplicationV1(
            park_reference=reference.reference,
            record_type=_required_fixture(html, r'data-peak-type="([^"]+)"', "type"),
            proposal_summary=unescape(
                _required_fixture(html, r'data-peak-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required_fixture(
                html, r'data-peak-status="([^"]+)"', "status"
            ),
            parish=_required_fixture(html, r'data-peak-parish="([^"]+)"', "parish"),
            legacy_record_url=HttpUrl(
                _required_fixture(html, r'data-peak-legacy="([^"]+)"', "legacy URL")
            ),
        )
        return _snapshot(reference, payload, detail)

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        if reference.source_id != LEGACY_SOURCE or reference.locator is None:
            raise PeakDistrictRoutingError(reference.reference)
        locator = urlsplit(reference.locator)
        applications = parse_qs(locator.query).get("applicationNumber", [])
        if (
            locator.scheme != "https"
            or locator.netloc != "planning.peakdistrict.gov.uk"
            or locator.path != urlsplit(f"{_ONLINE_BASE}/OnlinePlanningOverview").path
            or len(applications) != 1
        ):
            raise PeakDistrictRoutingError(reference.reference)
        if applications[0] != reference.reference:
            raise PeakDistrictReferenceMismatchError(
                reference.reference,
                applications[0],
            )
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(reference.locator), intent=RequestIntent.DETAIL)
        )
        parsed = _parse_assure_detail(detail.body)
        if parsed.reference != reference.reference:
            raise PeakDistrictReferenceMismatchError(
                reference.reference,
                parsed.reference,
            )
        evidence = [detail]
        documents, documents_state = await _fetch_assure_documents(
            session,
            parsed.documents_endpoint,
            reference.reference,
            evidence,
        )
        payload = PeakDistrictApplicationV1(
            park_reference=parsed.reference,
            record_type=parsed.record_type,
            proposal_summary=parsed.proposal,
            case_status=parsed.status,
            parish=parsed.parish,
            legacy_record_url=HttpUrl(reference.locator),
            development_address=parsed.address,
            validated_date=parsed.registered_date,
            assurelive_url=HttpUrl(reference.locator),
            applicant_name=parsed.applicant_name,
            agent_name=parsed.agent_name,
            officer_name=parsed.officer_name,
            documents=documents,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=documents_state,
                comments=(
                    FailedSection(code="comments-tab-unsupported")
                    if parsed.comments_tab
                    else UnavailableSection(
                        reason="AssureLive exposes no public comments tab"
                    )
                ),
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[PeakDistrictApplicationV1],
    ) -> NormalisedObservation:
        """Map Peak District visible summary values to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_summary,
            status=payload.case_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=document.title, url=document.url)
                for document in payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="peak-district-v3",
            metadata=ApplicationMetadata(
                application_type=payload.record_type,
                address=payload.development_address,
                validated_date=payload.validated_date,
                aliases=(
                    ()
                    if payload.planning_portal_reference is None
                    else (payload.planning_portal_reference,)
                ),
                source_url=snapshot.evidence[0].url,
                published_parties=tuple(
                    value
                    for value in (payload.applicant_name, payload.agent_name)
                    if value is not None
                ),
                officer_name=payload.officer_name,
            ),
        )


def peak_district_query_inventory(
    window: DiscoveryWindow,
) -> tuple[PeakDistrictQueryV1, ...]:
    """Return the complete ordered AssureLive query plan for one scope."""
    date_value = f"{window.start:%d/%m/%Y}..{window.end:%d/%m/%Y}"
    bounded = tuple(
        PeakDistrictQueryV1(
            kind="bounded-date",
            field=field,
            value=date_value,
        )
        for field in _DATE_QUERY_FIELDS
    )
    if not window.include_open:
        return bounded
    return (
        *bounded,
        *(
            PeakDistrictQueryV1(
                kind="older-open",
                field=_STATUS_FIELD,
                value=status,
            )
            for status in _OPEN_STATUSES
        ),
    )


def _parse_search_form(
    search_body: bytes, advanced_body: bytes
) -> tuple[FormField, ...]:
    search_soup = BeautifulSoup(search_body, "html.parser")
    form = search_soup.select_one("form#frmOnlinePlanningSearch")
    if not isinstance(form, Tag):
        _raise_parse("search form")
    expected_routes = {
        "urlOnlinePlanningSearchResult": urlsplit(_RESULTS_URL).path,
        "urlOnlinePlanningAdvanceSearchView": urlsplit(_ADVANCED_FORM_URL).path,
    }
    for element_id, expected in expected_routes.items():
        control = form.select_one(f"#{element_id}")
        if not isinstance(control, Tag) or str(control.get("value", "")) != expected:
            _raise_parse(f"search route {element_id}")
    advanced_soup = BeautifulSoup(advanced_body, "html.parser")
    status = advanced_soup.select_one(f'select[name="{_STATUS_FIELD}"]')
    if not isinstance(status, Tag):
        _raise_parse("open status options")
    options = tuple(
        str(option.get("value", "")) for option in status.select("option[value]")
    )
    if any(options.count(value) != 1 for value in ("-1", *_OPEN_STATUSES)):
        _raise_parse("open status options")
    for field in _DATE_QUERY_FIELDS:
        names = {
            str(item.get("name", "")) for item in advanced_soup.select("input[name]")
        }
        any_time = advanced_soup.select(
            f'input[type="radio"][name="AdvanceSearch.{field}AnyTime"]'
            '[value="False"][checked]'
        )
        between = advanced_soup.select(
            f'input[type="radio"][name="AdvanceSearch.{field}Between"]'
            '[value="True"]:not([checked])'
        )
        if (
            len(any_time) != 1
            or len(between) != 1
            or not {
                f"AdvanceSearch.{field}AnyTime",
                f"AdvanceSearch.{field}Between",
                f"AdvanceSearch.{field}FromDate",
                f"AdvanceSearch.{field}ToDate",
            }.issubset(names)
        ):
            _raise_parse(f"advanced date field {field}")
    return (*_successful_controls(form), *_successful_controls(advanced_soup))


def _successful_controls(container: Tag) -> tuple[FormField, ...]:
    fields = []
    for control in container.select("input[name], select[name], textarea[name]"):
        if control.has_attr("disabled"):
            continue
        name = str(control.get("name"))
        if control.name == "input":
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "file", "image", "reset", "submit"}:
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


def _replace_fields(
    fields: tuple[FormField, ...],
    values: dict[str, str],
    *,
    remove: tuple[str, ...] = (),
) -> tuple[FormField, ...]:
    removed = {*values, *remove}
    retained = tuple(field for field in fields if field.name not in removed)
    return (
        *retained,
        *(FormField(name=name, value=value) for name, value in values.items()),
    )


def _query_request(
    form: tuple[FormField, ...],
    query: PeakDistrictQueryV1,
) -> PortalRequest:
    overrides: dict[str, str] = {
        "SearchFor": "PlanningApplications",
        "IsAdvanceSearch": "true",
        "IsPaginationClicked": "false",
        "PagingParameters.CurrentPageIndex": "0",
        "PagingParameters.PageSize": "20",
        "PagingParameters.TotalRecords": "0",
        _STATUS_FIELD: "-1",
    }
    remove: tuple[str, ...] = ()
    if query.kind == "bounded-date":
        start, separator, end = query.value.partition("..")
        if separator != ".." or query.field not in _DATE_QUERY_FIELDS:
            _raise_parse("bounded query")
        overrides.update(
            {
                f"AdvanceSearch.{query.field}Between": "True",
                f"AdvanceSearch.{query.field}FromDate": start,
                f"AdvanceSearch.{query.field}ToDate": end,
            }
        )
        remove = (f"AdvanceSearch.{query.field}AnyTime",)
    else:
        if query.field != _STATUS_FIELD or query.value not in _OPEN_STATUSES:
            _raise_parse("open query")
        overrides[_STATUS_FIELD] = query.value
    return PortalRequest(
        url=HttpUrl(_RESULTS_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_replace_fields(form, overrides, remove=remove),
    )


def _pagination_request(
    form: tuple[FormField, ...],
    page_form: tuple[FormField, ...] | None,
    page_index: int,
) -> PortalRequest:
    if page_form is None:
        _raise_parse("pagination form")
    page_values = {field.name: field.value for field in page_form}
    fields = _replace_fields(form, page_values)
    return PortalRequest(
        url=HttpUrl(_PAGINATION_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_replace_fields(
            fields,
            {
                "PagingParameters.CurrentPageIndex": str(page_index),
                "IsPaginationClicked": "true",
            },
        ),
    )


def _parse_search_page(body: bytes, *, expected_page: int) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
    pagination = soup.select_one("#generalSearchPagination")
    if (
        not isinstance(pagination, Tag)
        or urljoin(f"{ASSURE_BASE}/", str(pagination.get("data-url", "")))
        != _PAGINATION_URL
    ):
        _raise_parse("search results")
    total_match = _TOTAL_PATTERN.search(soup.get_text(" ", strip=True))
    if total_match is None:
        _raise_parse("reported result count")
    reported = int(total_match.group(1))
    page_index = _required_control_int(soup, "PagingParameters.CurrentPageIndex")
    page_size = _required_control_int(soup, "PageSize")
    hidden_reported = _required_control_int(soup, "TotalRecords")
    page_count = _required_control_int(soup, "PageCount")
    if reported != hidden_reported:
        raise PeakDistrictCountMismatchError(reported, hidden_reported)
    if page_index != expected_page:
        _raise_parse("result page index")
    if page_size <= 0:
        _raise_parse("result page size")
    references = _result_references(soup)
    remaining = reported - (page_index * page_size)
    expected_rows = min(page_size, max(0, remaining))
    if len(references) != expected_rows:
        raise PeakDistrictCountMismatchError(expected_rows, len(references))
    pages = max(1, (reported + page_size - 1) // page_size)
    if page_count != pages:
        raise PeakDistrictCountMismatchError(pages, page_count)
    observed_pages = {
        int(match.group(1))
        for link in soup.select('a[onclick*="PagingClick"]')
        if (match := _PAGE_PATTERN.search(str(link.get("onclick", "")))) is not None
    }
    if observed_pages != set(range(pages)):
        _raise_parse("result pagination inventory")
    return _SearchPage(
        references=references,
        reported=reported,
        page_index=page_index,
        page_size=page_size,
        form=_successful_controls(soup),
    )


def _required_control_int(soup: BeautifulSoup, name: str) -> int:
    values = {
        str(control.get("value", ""))
        for control in soup.select(f'[name="{name}"]')
        if control.name != "select"
    }
    values.update(
        str(option.get("value", ""))
        for select in soup.select(f'select[name="{name}"]')
        for option in [
            select.select_one("option[selected]") or select.select_one("option")
        ]
        if option is not None
    )
    if len(values) != 1:
        _raise_parse(f"result control {name}")
    try:
        return int(values.pop())
    except ValueError:
        return _raise_parse(f"result control {name}")


def _result_references(container: Tag) -> tuple[SourceReference, ...]:
    found: dict[str, SourceReference] = {}
    for link in container.select('a[href*="OnlinePlanningOverview"]'):
        href = urljoin(f"{ASSURE_BASE}/", str(link.get("href", "")))
        applications = parse_qs(urlsplit(href).query).get("applicationNumber", [])
        if len(applications) != 1 or not applications[0]:
            _raise_parse("result application number")
        reference = applications[0].strip()
        row = link.find_parent(class_="row")
        if not isinstance(row, Tag):
            _raise_parse("result row")
        displayed_match = _APPLICATION_NUMBER_PATTERN.search(
            row.get_text(" ", strip=True)
        )
        if displayed_match is None:
            _raise_parse("result reference")
        displayed = displayed_match.group(1).strip()
        if displayed != reference:
            raise PeakDistrictReferenceMismatchError(displayed, reference)
        candidate = SourceReference(
            source_id=LEGACY_SOURCE,
            reference=reference,
            locator=href,
        )
        current = found.get(reference)
        if current is not None and current.locator != candidate.locator:
            _raise_parse("conflicting result locators")
        found[reference] = candidate
    return tuple(found.values())


def _advance_checkpoint(
    progress: PeakDistrictCheckpointV1,
    *,
    active_page: _ActivePage,
    search_page: _SearchPage,
    all_query_keys: tuple[str, ...],
) -> tuple[PeakDistrictCheckpointV1, tuple[SourceReference, ...], bool]:
    next_row_count = active_page.row_count + len(search_page.references)
    if next_row_count > search_page.reported or (
        not search_page.references and next_row_count < search_page.reported
    ):
        raise PeakDistrictCountMismatchError(search_page.reported, next_row_count)
    seen = set(progress.seen_references)
    fresh = []
    for reference in search_page.references:
        if reference.reference not in seen:
            seen.add(reference.reference)
            fresh.append(reference)
    last_page = next_row_count == search_page.reported
    if last_page:
        completed_queries = (*progress.completed_queries, active_page.query_key)
        checkpoint = progress.model_copy(
            update={
                "completed_queries": completed_queries,
                "active_query": None,
                "next_page_index": 0,
                "query_row_count": 0,
                "seen_references": tuple(seen),
                "live_complete": len(completed_queries) == len(all_query_keys),
            }
        )
    else:
        checkpoint = progress.model_copy(
            update={
                "active_query": active_page.query_key,
                "next_page_index": active_page.page_index + 1,
                "query_row_count": next_row_count,
                "seen_references": tuple(seen),
            }
        )
    return checkpoint, tuple(fresh), last_page


def _assert_checkpoint(
    checkpoint: PeakDistrictCheckpointV1,
    query_keys: tuple[str, ...],
) -> None:
    completed = checkpoint.completed_queries
    if (
        len(completed) != len(set(completed))
        or completed != query_keys[: len(completed)]
        or (
            checkpoint.active_query is not None
            and (
                checkpoint.active_query not in query_keys
                or checkpoint.active_query in completed
            )
        )
        or len(checkpoint.seen_references) != len(set(checkpoint.seen_references))
        or (
            checkpoint.live_complete
            and (
                completed != query_keys
                or checkpoint.active_query is not None
                or checkpoint.next_page_index != 0
                or checkpoint.query_row_count != 0
            )
        )
    ):
        raise PeakDistrictCheckpointError


def _parse_assure_detail(body: bytes) -> _AssureDetail:
    soup = BeautifulSoup(body, "html.parser")
    reference = _required_element_text(soup, "#spnApplicationId", "detail reference")
    hidden = soup.select_one("#applicationReference")
    if not isinstance(hidden, Tag) or str(hidden.get("value", "")).strip() != reference:
        _raise_parse("detail application reference")
    status = _required_element_text(
        soup,
        "#applicationStatusHelpTextHeader",
        "detail status",
    )
    labels = soup.select(
        ".row.btspace.tpspace .col-xs-12.padding-0 > .col-xs-12 > label"
    )
    record_type = (
        labels[0].get_text(" ", strip=True)
        if labels and labels[0].get("id") != "applicationDisplayAddress"
        else ""
    )
    if not record_type:
        _raise_parse("detail record type")
    fields: dict[str, str] = {}
    for row in soup.select("#tabOverviewMain table.boxBorderLightGrey tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) >= _MINIMUM_LABELLED_CELLS:
            key = _normalise_label(cells[0].get_text(" ", strip=True))
            value = cells[-1].get_text(" ", strip=True)
            if key and value:
                fields[key] = value
    proposal = _required_field(fields, "proposal")
    parish = _required_field(fields, "parish")
    address_element = soup.select_one("#applicationDisplayAddress")
    address = (
        None
        if not isinstance(address_element, Tag)
        else " ".join(address_element.get_text(" ", strip=True).split()) or None
    )
    documents_element = soup.select_one("#divDisplayDocumentsUrl")
    documents_endpoint = (
        None
        if not isinstance(documents_element, Tag)
        else str(documents_element.get("data-url", "")) or None
    )
    return _AssureDetail(
        reference=reference,
        record_type=record_type,
        proposal=proposal,
        status=status,
        parish=parish,
        address=address,
        registered_date=_optional_date(fields, "registered"),
        applicant_name=_optional_field(fields, "applicant"),
        agent_name=_optional_field(fields, "agent/company", "agent"),
        officer_name=_optional_field(fields, "planning officer", "case officer"),
        documents_endpoint=documents_endpoint,
        comments_tab=soup.select_one("#Comments_tab") is not None,
    )


def _required_element_text(
    soup: BeautifulSoup,
    selector: str,
    field: str,
) -> str:
    element = soup.select_one(selector)
    if not isinstance(element, Tag):
        _raise_parse(field)
    value = element.get_text(" ", strip=True)
    if not value:
        _raise_parse(field)
    return value


async def _fetch_assure_documents(
    session: PortalSession,
    endpoint: str | None,
    reference: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[PeakDistrictDocumentV1, ...], SectionState]:
    if endpoint is None or urljoin(f"{ASSURE_BASE}/", endpoint) != _DOCUMENTS_URL:
        return (), FailedSection(code="documents-route-drift")
    documents: list[PeakDistrictDocumentV1] = []
    seen_urls: set[str] = set()
    page_index = 0
    while True:
        try:
            capture = await session.fetch(
                _document_request(reference, page_index),
            )
        except SourceUnavailableError:
            return (), FailedSection(code="source-unavailable")
        evidence.append(capture)
        try:
            page = _parse_document_page(
                capture.body,
                reference=reference,
                expected_page=page_index,
            )
        except PeakDistrictParseError as error:
            return (), FailedSection(code=error.code)
        except PeakDistrictCountMismatchError:
            return (), FailedSection(code="document-count-mismatch")
        for document in page.documents:
            url = str(document.url)
            if url in seen_urls:
                return (), FailedSection(code="duplicate-document-url")
            seen_urls.add(url)
            documents.append(document)
        if len(documents) == page.reported:
            return tuple(documents), collection_state(len(documents))
        page_index += 1


def _document_request(reference: str, page_index: int) -> PortalRequest:
    query = urlencode(
        {
            "applicationNumber": reference,
            "currentPageIndex": page_index,
            "IsDatePublishSortedDescending": "false",
            "pageSize": 10,
        }
    )
    return PortalRequest(
        url=HttpUrl(f"{_DOCUMENTS_URL}?{query}"),
        intent=RequestIntent.DETAIL,
        method=RequestMethod.POST,
    )


def _parse_document_page(
    body: bytes,
    *,
    reference: str,
    expected_page: int,
) -> _DocumentPage:
    soup = BeautifulSoup(body, "html.parser")
    container = soup.select_one("#tabDocumentsMain")
    if not isinstance(container, Tag):
        _raise_parse("documents container")
    links = container.select(f'a[href*="{_DOCUMENT_PATH_PREFIX}"]')
    if "no record(s) found" in container.get_text(" ", strip=True).casefold():
        if links or expected_page != 0:
            _raise_parse("documents empty state")
        return _DocumentPage(
            documents=(),
            reported=0,
            page_index=0,
            page_size=10,
        )
    total_match = _TOTAL_PATTERN.search(container.get_text(" ", strip=True))
    if total_match is None:
        _raise_parse("document reported count")
    reported = int(total_match.group(1))
    document_count = _required_control_int(soup, "DocumentCount")
    hidden_reported = _required_control_int(soup, "PagingParameters.TotalRecords")
    if len({reported, document_count, hidden_reported}) != 1:
        raise PeakDistrictCountMismatchError(reported, hidden_reported)
    page_index = _required_control_int(soup, "PagingParameters.CurrentPageIndex")
    page_size = _required_control_int(soup, "PagingParameters.PageSize")
    if page_index != expected_page:
        _raise_parse("document page index")
    if page_size <= 0:
        _raise_parse("document page size")
    documents = tuple(_parse_document_link(link, reference) for link in links)
    expected_rows = min(page_size, max(0, reported - (page_index * page_size)))
    if len(documents) != expected_rows:
        raise PeakDistrictCountMismatchError(expected_rows, len(documents))
    pages = max(1, (reported + page_size - 1) // page_size)
    observed_pages = {
        int(match.group(1))
        for link in container.select(".pagination a[onclick]")
        if (match := _PAGE_PATTERN.search(str(link.get("onclick", "")))) is not None
    }
    if observed_pages != set(range(pages)):
        _raise_parse("document pagination inventory")
    return _DocumentPage(
        documents=documents,
        reported=reported,
        page_index=page_index,
        page_size=page_size,
    )


def _parse_document_link(link: Tag, reference: str) -> PeakDistrictDocumentV1:
    href = urljoin(f"{ASSURE_BASE}/", str(link.get("href", "")))
    split = urlsplit(href)
    query = parse_qs(split.query)
    if (
        split.scheme != "https"
        or split.netloc != "planning.peakdistrict.gov.uk"
        or not split.path.startswith(_DOCUMENT_PATH_PREFIX)
        or query.get("applicationNumber") != [reference]
        or any(
            len(query.get(name, [])) != 1
            for name in ("FileName", "fileType", "aspectGuid")
        )
    ):
        _raise_parse("document metadata link")
    row = link.find_parent(class_="row")
    if not isinstance(row, Tag):
        _raise_parse("document metadata row")
    cells = row.find_all("div", recursive=False)
    if len(cells) < _DOCUMENT_COLUMN_COUNT:
        _raise_parse("document metadata row")
    title = link.get_text(" ", strip=True)
    published = _parse_date(cells[0].get_text(" ", strip=True))
    document_type = cells[-1].get_text(" ", strip=True)
    if not title or published is None or not document_type:
        _raise_parse("document metadata")
    return PeakDistrictDocumentV1(
        title=title,
        url=HttpUrl(href),
        published_date=published,
        document_type=document_type,
    )


def _snapshot(
    reference: SourceReference,
    payload: PeakDistrictApplicationV1,
    detail: EvidenceCapture,
) -> NativeSnapshot[PeakDistrictApplicationV1]:
    return NativeSnapshot(
        reference=reference,
        observed_at=datetime.now(UTC),
        payload=payload,
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=UnavailableSection(
                reason="fixture does not include document metadata"
            ),
            comments=UnavailableSection(
                reason="fixture does not include public comments"
            ),
        ),
        evidence=(detail,),
    )


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
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        _raise_parse(f"date {'/'.join(names)}")
    return parsed


def _parse_date(value: str) -> date | None:
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class PeakDistrictParseError(ValueError):
    """A required Peak District boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Peak District field {field}")


class PeakDistrictCountMismatchError(ValueError):
    """The DataTable count did not match all DOM rows."""

    def __init__(self, expected: int, actual: int) -> None:
        """Report only counts."""
        super().__init__(
            f"Peak District reported {expected} results but exposed {actual}"
        )


class PeakDistrictCheckpointError(ValueError):
    """A saved cursor is incoherent with its exact discovery scope."""


class PeakDistrictRoutingError(ValueError):
    """A reference lacks the opaque legacy result locator."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Peak District cannot route reference {reference}")


class PeakDistrictReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(
            f"expected Peak District reference {expected}, received {actual}"
        )


def _raise_parse(field: str) -> NoReturn:
    raise PeakDistrictParseError(field)
