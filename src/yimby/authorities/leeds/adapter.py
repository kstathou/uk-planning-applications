# Copyright (c) 2026 Kostas Stathoulopoulos

"""Leeds-owned fixture and live weekly IDOX adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qs, quote, urlsplit

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
_PAGED_RESULTS_URL = f"{BASE_URL}/pagedSearchResults.do"
_DATE_TYPES = ("DC_Validated", "DC_Decided")
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")


class LeedsCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable Leeds weekly-list progress."""

    result_page: str
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    query_row_count: int = 0
    seen_references: tuple[str, ...] = ()
    live_complete: bool = False


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

    async def _discover_live(  # noqa: C901, PLR0912
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: LeedsCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[LeedsCheckpointV1]]:
        progress = checkpoint or LeedsCheckpointV1(result_page="live")
        if window.include_open and progress.live_complete:
            raise LeedsOpenEnumerationUnsupportedError
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_WEEKLY_FORM_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_form(form_capture.body)
        query_keys = tuple(
            f"{week}|{date_type}"
            for week in _intersecting_weeks(form, window)
            for date_type in _DATE_TYPES
        )
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            raise LeedsCheckpointError(progress.active_query)
        pending = tuple(
            query for query in query_keys if query not in progress.completed_queries
        )
        for query in pending:
            week, date_type = query.split("|", maxsplit=1)
            page = progress.next_page if progress.active_query == query else 1
            row_count = (
                progress.query_row_count if progress.active_query == query else 0
            )
            if progress.active_query == query and page > 1:
                await session.fetch(_weekly_request(form, week, date_type, 1))
            while True:
                capture = await session.fetch(
                    _weekly_request(form, week, date_type, page)
                )
                search_page = _parse_search_page(capture.body)
                row_count += len(search_page.references)
                if row_count > search_page.reported or (
                    not search_page.references and row_count < search_page.reported
                ):
                    raise LeedsCountMismatchError(
                        query, search_page.reported, row_count
                    )
                seen = set(progress.seen_references)
                fresh = []
                for reference in search_page.references:
                    if reference.reference not in seen:
                        seen.add(reference.reference)
                        fresh.append(reference)
                last_page = row_count == search_page.reported
                if last_page:
                    completed_queries = (*progress.completed_queries, query)
                    next_checkpoint = progress.model_copy(
                        update={
                            "completed_queries": completed_queries,
                            "active_query": None,
                            "next_page": 1,
                            "query_row_count": 0,
                            "seen_references": tuple(seen),
                            "live_complete": (
                                len(completed_queries) == len(query_keys)
                                and not window.include_open
                            ),
                        }
                    )
                else:
                    next_checkpoint = progress.model_copy(
                        update={
                            "active_query": query,
                            "next_page": page + 1,
                            "query_row_count": row_count,
                            "seen_references": tuple(seen),
                        }
                    )
                yield DiscoveryBatch(
                    references=tuple(fresh),
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                )
                progress = next_checkpoint
                if last_page:
                    break
                page += 1
        if not pending and not window.include_open:
            completed_checkpoint = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(), next_checkpoint=completed_checkpoint, complete=True
            )
            return
        if window.include_open:
            raise LeedsOpenEnumerationUnsupportedError

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


def _parse_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form")
    if not isinstance(form, Tag):
        _raise_parse("form")
    if not any(field.name == "_csrf" and field.value for field in _form_fields(form)):
        _raise_parse("_csrf")
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
        reported = _reported_count(soup)
    except LeedsParseError:
        if not _is_uncounted_terminal_first_page(soup, row_count=len(references)):
            raise
        reported = len(references)
    return _SearchPage(references=tuple(references), reported=reported)


def _reported_count(soup: BeautifulSoup) -> int:
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
        if not 1 <= first <= last <= total:
            _raise_parse("reported result count")
        showing_ranges.append((first, last, total))
    if showing_ranges:
        displayed_range = showing_ranges[0]
        if any(value != displayed_range for value in showing_ranges[1:]):
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
