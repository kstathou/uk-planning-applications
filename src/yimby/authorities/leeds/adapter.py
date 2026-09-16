# Copyright (c) 2026 Kostas Stathoulopoulos

"""Leeds-owned fixture and live weekly IDOX adapter."""

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
    collection_state,
)
from yimby.transport import (
    FormField,
    PortalRequest,
    RequestIntent,
    RequestMethod,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("leeds-idox-public-access")
BASE_URL = "https://publicaccess.leeds.gov.uk/online-applications"
_WEEKLY_FORM_URL = f"{BASE_URL}/search.do?action=weeklyList"
_WEEKLY_RESULTS_URL = f"{BASE_URL}/weeklyListResults.do?action=firstPage"
_ADVANCED_FORM_URL = f"{BASE_URL}/search.do?action=advanced"
_ADVANCED_RESULTS_URL = f"{BASE_URL}/advancedSearchResults.do?action=firstPage"
_PAGED_RESULTS_URL = f"{BASE_URL}/pagedSearchResults.do"
_DATE_TYPES: tuple[Literal["DC_Validated", "DC_Decided"], ...] = (
    "DC_Validated",
    "DC_Decided",
)
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_TOO_MANY_RESULTS = "too many results found. please enter some more parameters."
_CASE_TYPES = (
    ("DAG", "Agricultural Determination"),
    ("ADV", "Application to Display Adverts"),
    ("CLA", "Certificate Alternative Appropriate Dev"),
    ("CLE", "Certificate of Existing Lawful Use"),
    ("CLP", "Certificate of Proposed Lawful Use"),
    ("DEM", "Demolition Notification"),
    ("COND", "Discharge of Conditions"),
    ("EXT", "Extension of Time Period"),
    ("EDD", "Extension to Determination Date"),
    ("BNG106", "Floating S106/BNG"),
    ("FU", "Full Planning Application"),
    ("HAZ", "Hazardous Substance Consent"),
    ("DHH", "Householder Determination"),
    ("LI", "Listed Building Application"),
    ("LA", "Local Authority Application Reg 4(1)"),
    ("LATR", "Local Authority Tree Works"),
    ("S106", "Modify or Discharge S106 Agreement"),
    ("MOD", "Non Material Amendment"),
    ("N1490", "Notification of Overhead Line"),
    ("NPD", "Notification under Permitted Development"),
    ("OT", "Outline Planning Application"),
    ("DPD", "Permitted Development Determination"),
    ("PIP", "Planning Permission in Principle"),
    ("PRESME", "Pre-Application (SME Builders)"),
    ("RM", "Reserved Matters Application"),
    ("TDC", "Technical Details Consent"),
    ("DTM", "Telecommunications Determination"),
    ("TWA", "Transport and Works Act 1992"),
    ("TR", "Tree Works"),
    ("UNK", "Unknown"),
)


class LeedsDiscoveryScope(FrozenModel):
    """Exact live discovery request owning resumable Leeds progress."""

    start: date
    end: date
    include_open: bool


class LeedsReferenceIdentityV1(FrozenModel):
    """Stable Leeds reference and source-local locator pair."""

    reference: str
    locator: str


class LeedsCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable Leeds weekly-list progress."""

    result_page: str
    live_scope: LeedsDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    query_totals: tuple[int, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    query_row_count: int = 0
    seen_references: tuple[str, ...] = ()
    seen_identities: tuple[LeedsReferenceIdentityV1, ...] = ()
    live_complete: bool = False


class _WeeklyQuery(FrozenModel):
    week: str
    date_type: Literal["DC_Validated", "DC_Decided"]

    @property
    def key(self) -> str:
        return f"{self.week}|{self.date_type}"


class _DateRangeQuery(FrozenModel):
    kind: Literal["validated", "decision"]
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"advanced|{self.kind}|{self.start.isoformat()}|{self.end.isoformat()}"


class _CurrentCaseTypeQuery(FrozenModel):
    case_type: str

    @property
    def key(self) -> str:
        return f"advanced|current|{self.case_type}"


class _ActiveAppealQuery(FrozenModel):
    appeal_status: Literal["Appeal lodged"] = "Appeal lodged"

    @property
    def key(self) -> str:
        return f"advanced|appeal|{self.appeal_status}"


type _AdvancedQuery = _DateRangeQuery | _CurrentCaseTypeQuery | _ActiveAppealQuery


class LeedsDocumentV1(FrozenModel):
    """Leeds document metadata without attachment bodies."""

    title: str
    url: HttpUrl
    published_date: date | None = None
    document_type: str | None = None
    drawing_number: str | None = None
    description: str | None = None
    source_links: tuple[HttpUrl, ...] = ()


class LeedsApplicationV1(FrozenModel):
    """Leeds-native IDOX application."""

    idox_key: str
    application_reference: str
    proposal_text: str
    case_status: str
    documents: tuple[LeedsDocumentV1, ...]
    application_type: str


class LeedsAdapter:
    """Own Leeds request, parsing, completeness, and normalisation rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("leeds"),
        name="Leeds City Council",
        kind=AuthorityKind.METROPOLITAN,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: LeedsCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[LeedsCheckpointV1]]:
        """Use fixture discovery or Leeds's captured weekly IDOX flow."""
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
        checkpoint: LeedsCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[LeedsCheckpointV1]]:
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
            for value in re.findall(r'data-leeds-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(html, r'data-leeds-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=LeedsCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(  # noqa: C901, PLR0912, PLR0915
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: LeedsCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[LeedsCheckpointV1]]:
        requested_scope = LeedsDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != requested_scope:
            progress = LeedsCheckpointV1(
                result_page="live",
                live_scope=requested_scope,
            )
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        weekly_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_WEEKLY_FORM_URL), intent=RequestIntent.SEARCH)
        )
        weekly_form = _parse_form(weekly_capture.body)
        weekly_queries = tuple(
            _WeeklyQuery(week=week, date_type=date_type)
            for week in _intersecting_weeks(weekly_form, window)
            for date_type in _DATE_TYPES
        )
        advanced_queries = _advanced_query_inventory(window)
        query_keys = tuple(query.key for query in weekly_queries) + tuple(
            query.key for query in advanced_queries
        )
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            raise LeedsCheckpointError(progress.active_query)
        pending_weekly = tuple(
            query
            for query in weekly_queries
            if query.key not in progress.completed_queries
        )
        for query in pending_weekly:
            page = progress.next_page if progress.active_query == query.key else 1
            row_count = (
                progress.query_row_count if progress.active_query == query.key else 0
            )
            if progress.active_query == query.key and page > 1:
                await session.fetch(
                    _weekly_request(
                        weekly_form,
                        query.week,
                        query.date_type,
                        1,
                    )
                )
            while True:
                capture = await session.fetch(
                    _weekly_request(
                        weekly_form,
                        query.week,
                        query.date_type,
                        page,
                    )
                )
                search_page = _parse_search_page(capture.body)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=query.key,
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
        if not advanced_queries:
            if pending_weekly:
                return
            completed = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(), next_checkpoint=completed, complete=True
            )
            return

        advanced_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_ADVANCED_FORM_URL), intent=RequestIntent.SEARCH)
        )
        advanced_form = _parse_advanced_form(advanced_capture.body)
        pending_advanced = tuple(
            query
            for query in advanced_queries
            if query.key not in progress.completed_queries
        )
        for query in pending_advanced:
            page = progress.next_page if progress.active_query == query.key else 1
            row_count = (
                progress.query_row_count if progress.active_query == query.key else 0
            )
            if progress.active_query == query.key and page > 1:
                await session.fetch(_advanced_request(advanced_form, query, 1))
            while True:
                capture = await session.fetch(
                    _advanced_request(advanced_form, query, page)
                )
                search_page = _parse_advanced_search_page(capture.body, page=page)
                next_checkpoint, fresh, last_page = _advance_checkpoint(
                    progress,
                    active_page=_ActivePage(
                        query_key=query.key,
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
            completed = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(), next_checkpoint=completed, complete=True
            )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[LeedsApplicationV1]:
        """Read fixture detail or Leeds live summary and exposed sections."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[LeedsApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{BASE_URL}/application/{encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        documents = tuple(
            LeedsDocumentV1(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-leeds-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = LeedsApplicationV1(
            idox_key=_required_fixture(html, r'data-leeds-key="([^"]+)"', "IDOX key"),
            application_reference=reference.reference,
            proposal_text=unescape(
                _required_fixture(html, r'data-leeds-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required_fixture(
                html, r'data-leeds-status="([^"]+)"', "status"
            ),
            application_type=_required_fixture(
                html, r'data-leeds-type="([^"]+)"', "application type"
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
    ) -> NativeSnapshot[LeedsApplicationV1]:
        if reference.locator is None:
            raise LeedsRoutingError(reference.reference)
        detail = await session.fetch(
            _detail_request(reference.locator, "summary", RequestIntent.DETAIL)
        )
        message = detail.body.decode(errors="replace").casefold()
        if "unable to perform this task" in message and "remote exception" in message:
            raise LeedsDetailUnavailableError
        raise LeedsDetailUnverifiedError

    def normalise(
        self,
        snapshot: NativeSnapshot[LeedsApplicationV1],
    ) -> NormalisedObservation:
        """Map Leeds-native fields to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_text,
            status=payload.case_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="leeds-v1",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
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


def _advance_checkpoint(
    progress: LeedsCheckpointV1,
    *,
    active_page: _ActivePage,
    search_page: _SearchPage,
    all_query_keys: tuple[str, ...],
) -> tuple[LeedsCheckpointV1, tuple[SourceReference, ...], bool]:
    next_row_count = active_page.row_count + len(search_page.references)
    if next_row_count > search_page.reported or (
        not search_page.references and next_row_count < search_page.reported
    ):
        raise LeedsCountMismatchError(
            active_page.query_key,
            search_page.reported,
            next_row_count,
        )
    identities = {
        identity.reference: identity.locator for identity in progress.seen_identities
    }
    seen = set(progress.seen_references)
    fresh = []
    for reference in search_page.references:
        if reference.locator is None:
            raise LeedsRoutingError(reference.reference)
        existing_locator = identities.get(reference.reference)
        if existing_locator is not None and existing_locator != reference.locator:
            raise LeedsIdentityConflictError(reference.reference)
        identities[reference.reference] = reference.locator
        if reference.reference not in seen:
            seen.add(reference.reference)
            fresh.append(reference)
    identity_values = tuple(
        LeedsReferenceIdentityV1(reference=reference, locator=locator)
        for reference, locator in sorted(identities.items())
    )
    last_page = next_row_count == search_page.reported
    if last_page:
        completed_queries = (*progress.completed_queries, active_page.query_key)
        checkpoint = progress.model_copy(
            update={
                "completed_queries": completed_queries,
                "query_totals": (*progress.query_totals, search_page.reported),
                "active_query": None,
                "next_page": 1,
                "query_row_count": 0,
                "seen_references": tuple(sorted(seen)),
                "seen_identities": identity_values,
                "live_complete": len(completed_queries) == len(all_query_keys),
            }
        )
    else:
        checkpoint = progress.model_copy(
            update={
                "active_query": active_page.query_key,
                "next_page": active_page.page + 1,
                "query_row_count": next_row_count,
                "seen_references": tuple(sorted(seen)),
                "seen_identities": identity_values,
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
    required_fields = {
        "_csrf",
        "searchCriteria.reference",
        "searchCriteria.description",
        "searchCriteria.applicantName",
        "searchCriteria.caseType",
        "searchCriteria.ward",
        "searchCriteria.parish",
        "searchCriteria.conservationArea",
        "searchCriteria.agent",
        "searchCriteria.caseStatus",
        "searchCriteria.caseDecision",
        "searchCriteria.appealStatus",
        "searchCriteria.developmentType",
        "caseAddressType",
        "searchCriteria.address",
        "date(applicationValidatedStart)",
        "date(applicationValidatedEnd)",
        "date(applicationCommitteeStart)",
        "date(applicationCommitteeEnd)",
        "date(applicationDecisionStart)",
        "date(applicationDecisionEnd)",
        "searchType",
    }
    if not required_fields.issubset(field.name for field in _form_fields(form)):
        _raise_parse("advanced form fields")
    _require_options(
        form,
        "searchCriteria.caseStatus",
        (
            ("", "All"),
            ("Current", "Current"),
            ("Decided", "Decided"),
            ("Unknown", "Unknown"),
        ),
        "case status",
    )
    _require_options(
        form,
        "searchCriteria.appealStatus",
        (
            ("", "All"),
            ("Appeal decided", "Appeal decided"),
            ("Appeal lodged", "Appeal lodged"),
            ("Unknown", "Unknown"),
        ),
        "appeal status",
    )
    _require_options(
        form,
        "searchCriteria.caseType",
        (("", "All"), *_CASE_TYPES),
        "case type",
    )
    values = {field.name: field.value for field in _form_fields(form)}
    if values["caseAddressType"] != "Application" or not values["searchType"]:
        _raise_parse("advanced form discriminators")
    return form


def _require_options(
    form: Tag,
    field: str,
    expected: tuple[tuple[str, str], ...],
    label: str,
) -> None:
    controls = form.select(f'select[name="{field}"]')
    if len(controls) != 1:
        _raise_parse(f"advanced {label}")
    actual = tuple(
        (str(option.get("value", "")), option.get_text(" ", strip=True))
        for option in controls[0].select("option[value]")
    )
    if actual != expected:
        _raise_parse(f"advanced {label} options")


def _advanced_query_inventory(window: DiscoveryWindow) -> tuple[_AdvancedQuery, ...]:
    if not window.include_open:
        return ()
    return (
        _DateRangeQuery(kind="validated", start=window.start, end=window.end),
        _DateRangeQuery(kind="decision", start=window.start, end=window.end),
        *(_CurrentCaseTypeQuery(case_type=value) for value, _label in _CASE_TYPES),
        _ActiveAppealQuery(),
    )


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
    if page > 1:
        return PortalRequest(
            url=HttpUrl(f"{_PAGED_RESULTS_URL}?action=page&searchCriteria.page={page}"),
            intent=RequestIntent.SEARCH,
        )
    values = {
        "searchCriteria.reference": "",
        "searchCriteria.description": "",
        "searchCriteria.applicantName": "",
        "searchCriteria.caseType": "",
        "searchCriteria.ward": "",
        "searchCriteria.parish": "",
        "searchCriteria.conservationArea": "",
        "searchCriteria.agent": "",
        "searchCriteria.caseStatus": "",
        "searchCriteria.caseDecision": "",
        "searchCriteria.appealStatus": "",
        "searchCriteria.developmentType": "",
        "searchCriteria.address": "",
        "date(applicationValidatedStart)": "",
        "date(applicationValidatedEnd)": "",
        "date(applicationCommitteeStart)": "",
        "date(applicationCommitteeEnd)": "",
        "date(applicationDecisionStart)": "",
        "date(applicationDecisionEnd)": "",
    }
    if isinstance(query, _DateRangeQuery):
        prefix = (
            "applicationValidated"
            if query.kind == "validated"
            else "applicationDecision"
        )
        values[f"date({prefix}Start)"] = query.start.strftime("%d/%m/%Y")
        values[f"date({prefix}End)"] = query.end.strftime("%d/%m/%Y")
    elif isinstance(query, _CurrentCaseTypeQuery):
        values["searchCriteria.caseStatus"] = "Current"
        values["searchCriteria.caseType"] = query.case_type
    else:
        values["searchCriteria.appealStatus"] = query.appeal_status
    return PortalRequest(
        url=HttpUrl(_ADVANCED_RESULTS_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_override_fields(form, values),
    )


def _parse_search_page(body: bytes) -> _SearchPage:
    soup = BeautifulSoup(body, "html.parser")
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
        reported = _reported_count(soup, row_count=len(references))
    except LeedsParseError:
        if not _is_uncounted_terminal_first_page(soup, row_count=len(references)):
            raise
        reported = len(references)
    return _SearchPage(references=tuple(references), reported=reported)


def _parse_advanced_search_page(body: bytes, *, page: int) -> _SearchPage:
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True).casefold()
    if _TOO_MANY_RESULTS in text:
        raise LeedsSearchCapError
    if page < 1:
        _raise_parse("advanced result page")
    return _parse_search_page(body)


def _reported_count(soup: BeautifulSoup, *, row_count: int) -> int:
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
        current_pages = tuple(
            int(str(control.get("value")))
            for control in soup.select('input[name="searchCriteria.page"][value]')
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


def _is_uncounted_terminal_first_page(
    soup: BeautifulSoup,
    *,
    row_count: int,
) -> bool:
    page_inputs = soup.select('input[name="searchCriteria.page"][value]')
    if (
        row_count == 0
        or len(page_inputs) != 1
        or str(page_inputs[0].get("value", "")).strip() != "1"
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
        except LeedsParseError:
            continue
    return _raise_parse(f"labelled {'/'.join(labels)}")


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


class LeedsParseError(ValueError):
    """A required Leeds boundary value was absent or inconsistent."""

    def __init__(self, field: str) -> None:
        """Name the safe boundary field without retaining response content."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Leeds field {field}")


def _raise_parse(field: str) -> NoReturn:
    raise LeedsParseError(field)


class LeedsCountMismatchError(LeedsParseError):
    """A Leeds displayed count disagreed with parsed records."""

    def __init__(self, section: str, expected: int, actual: int) -> None:
        """Describe the bounded count disagreement."""
        super().__init__(f"{section} count expected {expected} actual {actual}")


class LeedsSearchCapError(RuntimeError):
    """The official search refused to enumerate a complete partition."""

    def __init__(self) -> None:
        """Keep the source cap distinct from an empty result."""
        super().__init__("Leeds search exceeded the official result cap")


class LeedsIdentityConflictError(LeedsParseError):
    """One human reference acquired another source-local locator."""

    def __init__(self, reference: str) -> None:
        """Name the conflicting public reference."""
        super().__init__(f"identity conflict {reference}")


class LeedsOpenEnumerationUnsupportedError(RuntimeError):
    """Older-open Leeds enumeration lacks a proven bounded partition."""

    def __init__(self) -> None:
        """Prevent weekly discovery from claiming full bootstrap coverage."""
        super().__init__("Leeds older-open enumeration is not implemented")


class LeedsCheckpointError(ValueError):
    """A saved Leeds weekly query is absent from the current form."""

    def __init__(self, query: str) -> None:
        """Name the invalid query without exposing session values."""
        super().__init__(f"Leeds checkpoint query is unavailable: {query}")


class LeedsRoutingError(ValueError):
    """A Leeds live reference lacks its source-local keyVal."""

    def __init__(self, reference: str) -> None:
        """Name the unroutable human reference."""
        super().__init__(f"Leeds reference has no keyVal locator: {reference}")


class LeedsDetailUnavailableError(RuntimeError):
    """The verified Leeds detail route returned its remote-exception page."""

    def __init__(self) -> None:
        """Preserve a source failure instead of fabricating an empty record."""
        super().__init__("Leeds detail returned the recorded remote exception")


class LeedsDetailUnverifiedError(RuntimeError):
    """An unexpected Leeds detail response lacks a verified parser contract."""

    def __init__(self) -> None:
        """Refuse to parse a response beyond the recorded failure evidence."""
        super().__init__("Leeds live detail extraction is not verified")
