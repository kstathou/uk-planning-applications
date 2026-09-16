# Copyright (c) 2026 Kostas Stathoulopoulos

"""Barnet fixture and authority-native IDOX collection."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, NoReturn
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
_PAGED_RESULTS_URL = f"{BASE_URL}/pagedSearchResults.do"
_CURRENT_FORM_URL = f"{BASE_URL}/search.do?action=currentList"
_CURRENT_RESULTS_URL = f"{BASE_URL}/currentListResults.do?action=firstPage"
_DATE_TYPES = ("DC_Validated", "DC_Decided")
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
_MINIMUM_LABELLED_CELLS = 2


class BarnetCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable live weekly-list progress."""

    cursor: str
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    query_row_count: int = 0
    seen_references: tuple[str, ...] = ()
    live_complete: bool = False


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

    async def _discover_live(  # noqa: C901, PLR0912
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BarnetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BarnetCheckpointV1]]:
        progress = checkpoint or BarnetCheckpointV1(cursor="live")
        if progress.live_complete:
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
        query_keys = tuple(
            f"{week_value}|{date_type}"
            for week_value in _intersecting_weeks(form, window)
            for date_type in _DATE_TYPES
        )
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            raise BarnetCheckpointError(progress.active_query)
        pending = [
            query_key
            for query_key in query_keys
            if query_key not in progress.completed_queries
        ]
        for query_key in pending:
            week_value, date_type = query_key.split("|", maxsplit=1)
            page = progress.next_page if progress.active_query == query_key else 1
            row_count = (
                progress.query_row_count if progress.active_query == query_key else 0
            )
            if progress.active_query == query_key and page > 1:
                await session.fetch(_weekly_request(form, week_value, date_type, 1))
            while True:
                capture = await session.fetch(
                    _weekly_request(form, week_value, date_type, page)
                )
                search_page = _parse_search_page(capture.body)
                row_count += len(search_page.references)
                if page == search_page.page_count and row_count != search_page.reported:
                    raise BarnetCountMismatchError(
                        query_key,
                        search_page.reported,
                        row_count,
                    )
                seen = set(progress.seen_references)
                fresh = []
                for reference in search_page.references:
                    if reference.reference not in seen:
                        seen.add(reference.reference)
                        fresh.append(reference)
                if page == search_page.page_count:
                    completed = (*progress.completed_queries, query_key)
                    next_checkpoint = progress.model_copy(
                        update={
                            "completed_queries": completed,
                            "active_query": None,
                            "next_page": 1,
                            "query_row_count": 0,
                            "seen_references": tuple(seen),
                            "live_complete": (
                                len(completed) == len(query_keys)
                                and not window.include_open
                            ),
                        }
                    )
                else:
                    next_checkpoint = progress.model_copy(
                        update={
                            "active_query": query_key,
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
                if page == search_page.page_count:
                    break
                page += 1
        if not pending and not window.include_open:
            completed_checkpoint = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=completed_checkpoint,
                complete=True,
            )
            return
        if window.include_open:
            if not pending:
                yield DiscoveryBatch(
                    references=(),
                    next_checkpoint=progress,
                    complete=False,
                )
            await _probe_open_list(session)

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
    page_count: int


class _CommentTab(FrozenModel):
    active_tab: str
    category: str
    count_labels: tuple[str, ...]


def _parse_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form")
    if not isinstance(form, Tag):
        _raise_parse("form")
    fields = _form_fields(form)
    if not any(field.name == "_csrf" and field.value for field in fields):
        _raise_parse("_csrf")
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


def _intersecting_weeks(form: Tag, window: DiscoveryWindow) -> tuple[str, ...]:
    weeks: list[tuple[date, str]] = []
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


def _weekly_request(
    form: Tag,
    week: str,
    date_type: str,
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
    result_count = soup.select_one("[data-result-count]")
    if isinstance(result_count, Tag):
        raw_count = result_count.get("data-result-count")
        reported = int(str(raw_count))
    else:
        text = soup.get_text(" ", strip=True)
        if not references and "no results found" in text.casefold():
            reported = 0
        else:
            match = re.search(
                r"(?:showing\s+\d+\s*[-\N{EN DASH}]\s*\d+\s+of|"
                r"displaying.*?of|total)\s+(\d+)(?:\s+results?)?",
                text,
                re.IGNORECASE,
            )
            if match is None:
                _raise_parse("reported result count")
            reported = int(match.group(1))
    page_numbers = [1]
    for link in soup.select('a[href*="pagedSearchResults.do"]'):
        pages = parse_qs(urlsplit(str(link.get("href", ""))).query).get(
            "searchCriteria.page", []
        )
        page_numbers.extend(int(page) for page in pages if page.isdigit())
    return _SearchPage(
        references=tuple(references),
        reported=reported,
        page_count=max(page_numbers),
    )


async def _probe_open_list(session: PortalSession) -> None:
    form_capture = await session.fetch(
        PortalRequest(url=HttpUrl(_CURRENT_FORM_URL), intent=RequestIntent.SEARCH)
    )
    form = _parse_form(form_capture.body)
    result = await session.fetch(
        PortalRequest(
            url=HttpUrl(_CURRENT_RESULTS_URL),
            intent=RequestIntent.SEARCH,
            method=RequestMethod.POST,
            form=_form_fields(form),
        )
    )
    message = result.body.decode(errors="replace")
    if "Too many results found" in message:
        raise BarnetOpenListLimitError
    raise BarnetOpenEnumerationUnsupportedError


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
    if not tables:
        if expected == 0:
            return (), EmptySection()
        _raise_parse(f"{category} comments table")
    comments = []
    for table in tables:
        for index, (values, row) in enumerate(_table_rows(table), start=1):
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
    _assert_count(f"{category} comments", expected, len(comments))
    return tuple(comments), collection_state(len(comments))


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


class BarnetOpenListLimitError(RuntimeError):
    """The current-list route reported the observed unbounded result cap."""

    def __init__(self) -> None:
        """Expose an actionable bounded-search requirement."""
        super().__init__(
            "Barnet currentList is capped: Too many results found; "
            "bounded advanced-search partitions are required"
        )


class BarnetOpenEnumerationUnsupportedError(RuntimeError):
    """The current-list response cannot establish older-open completeness."""

    def __init__(self) -> None:
        """Prevent an unverified current list from being marked complete."""
        super().__init__("Barnet older-open enumeration is not implemented")


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
