# Copyright (c) 2026 Kostas Stathoulopoulos

"""Blackburn with Darwen-owned fixture and live Citizen portal adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from html import unescape
from typing import TYPE_CHECKING, NoReturn, Protocol, Self, runtime_checkable
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from pydantic import HttpUrl, model_validator

from yimby.domain import (
    ApplicationMetadata,
    AuthorityCapabilities,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CapabilityState,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    EvidenceCapture,
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
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bs4.element import Tag

    from yimby.transport import PortalSession

SOURCE = SourceId("blackburn-citizen-portal")
BASE_URL = "https://online.blackburn.gov.uk/planning"
_HISTORICAL_START = date(1977, 1, 1)
_RESULT_CAP = 30
_DETAIL_COLUMN_COUNT = 2
_RESULT_HEADERS = (
    "application reference",
    "application type",
    "location details",
    "proposal",
    "ward",
    "community",
    "decision",
    "view",
)


class BlackburnQueryKind(StrEnum):
    """One public search-form interpretation owned by Blackburn."""

    RECEIVED = "received"
    VALID = "valid"
    DECISION = "decision"
    OLDER_OPEN = "older-open"


class BlackburnDateRangeV1(FrozenModel):
    """One inclusive date range submitted to the public form."""

    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """Reject a range that cannot be submitted coherently."""
        if self.start > self.end:
            msg = "Blackburn query start must not follow its end"
            raise ValueError(msg)
        return self


class BlackburnQueryV1(FrozenModel):
    """One exact Blackburn form query."""

    kind: BlackburnQueryKind
    date_range: BlackburnDateRangeV1

    @property
    def key(self) -> str:
        """Return the stable receipt and checkpoint key."""
        return (
            f"{self.kind.value}|{self.date_range.start.isoformat()}|"
            f"{self.date_range.end.isoformat()}"
        )


class BlackburnDiscoveryScope(FrozenModel):
    """Exact caller scope that owns a live checkpoint."""

    start: date
    end: date
    include_open: bool


class BlackburnWithDarwenCheckpointV2(FrozenModel):
    """Fixture cursor plus resumable Citizen portal query inventory."""

    result_page: str = "live"
    live_scope: BlackburnDiscoveryScope | None = None
    pending_queries: tuple[BlackburnQueryV1, ...] = ()
    completed_queries: tuple[BlackburnQueryV1, ...] = ()
    split_queries: tuple[BlackburnQueryV1, ...] = ()
    seen_references: tuple[str, ...] = ()
    live_complete: bool = False


class BlackburnSearchRowV1(FrozenModel):
    """One public search result before application detail collection."""

    public_reference: str
    record_id: str
    application_type: str
    location: str
    proposal: str
    ward: str | None = None
    community: str | None = None
    decision: str | None = None


class BlackburnLocatorV1(FrozenModel):
    """Source-local numeric route paired with its public reference."""

    record_id: str
    public_reference: str


class BlackburnDocumentV1(FrozenModel):
    """Citizen portal document metadata without an attachment body."""

    title: str
    document_type: str
    published_date: date
    source_url: HttpUrl


class BlackburnWithDarwenApplicationV2(FrozenModel):
    """Blackburn-native public application and document metadata."""

    record_id: str
    council_reference: str
    development_proposal: str
    public_status: str
    application_type: str | None = None
    applicant: str | None = None
    agent: str | None = None
    location: str | None = None
    grid_reference: str | None = None
    ward: str | None = None
    community: str | None = None
    officer: str | None = None
    decision_level: str | None = None
    received_date: date | None = None
    valid_date: date | None = None
    expiry_date: date | None = None
    decision_date: date | None = None
    decision: str | None = None
    documents: tuple[BlackburnDocumentV1, ...] = ()


@runtime_checkable
class BlackburnPageSession(Protocol):
    """Authority-owned semantic browser operations."""

    async def search(self, query: BlackburnQueryV1) -> EvidenceCapture:
        """Submit one exact date query and retain rendered HTML."""

    async def application(self, locator: BlackburnLocatorV1) -> EvidenceCapture:
        """Render one application and its inline document metadata."""


class BlackburnWithDarwenAdapter:
    """Own Blackburn request, parsing, completeness, and normalisation rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("blackburn-with-darwen"),
        name="Blackburn with Darwen Borough Council",
        kind=AuthorityKind.UNITARY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
        capabilities=AuthorityCapabilities(
            discovery=CapabilityState.SUPPORTED,
            documents=CapabilityState.SUPPORTED,
            comments=CapabilityState.UNSUPPORTED,
            coordinates=CapabilityState.SUPPORTED,
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BlackburnWithDarwenCheckpointV2 | None,
    ) -> AsyncIterator[DiscoveryBatch[BlackburnWithDarwenCheckpointV2]]:
        """Use fixtures or exhaust the exact live query inventory."""
        if session.mode == TransportMode.FIXTURE:
            async for batch in self._discover_fixture(session, window, checkpoint):
                yield batch
            return
        if not isinstance(session, BlackburnPageSession):
            raise BlackburnBrowserSessionRequiredError
        async for batch in self._discover_live(session, window, checkpoint):
            yield batch

    async def _discover_fixture(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BlackburnWithDarwenCheckpointV2 | None,
    ) -> AsyncIterator[DiscoveryBatch[BlackburnWithDarwenCheckpointV2]]:
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
            for value in re.findall(r'data-blackburn-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(
            html,
            r'data-blackburn-page="([^"]+)"',
            "page",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=BlackburnWithDarwenCheckpointV2(result_page=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(
        self,
        session: BlackburnPageSession,
        window: DiscoveryWindow,
        checkpoint: BlackburnWithDarwenCheckpointV2 | None,
    ) -> AsyncIterator[DiscoveryBatch[BlackburnWithDarwenCheckpointV2]]:
        scope = BlackburnDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        if checkpoint is None or checkpoint.live_scope != scope:
            progress = BlackburnWithDarwenCheckpointV2(
                live_scope=scope,
                pending_queries=_initial_queries(window),
            )
        else:
            progress = checkpoint
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return

        while progress.pending_queries:
            query = progress.pending_queries[0]
            rows = _parse_search_rows((await session.search(query)).body)
            if len(rows) == _RESULT_CAP:
                first, second = _split_query(query)
                progress = progress.model_copy(
                    update={
                        "pending_queries": (
                            first,
                            second,
                            *progress.pending_queries[1:],
                        ),
                        "split_queries": (*progress.split_queries, query),
                    }
                )
                yield DiscoveryBatch(
                    references=(),
                    next_checkpoint=progress,
                    complete=False,
                )
                continue

            seen = set(progress.seen_references)
            seen_order = list(progress.seen_references)
            fresh = []
            for row in rows:
                if query.kind == BlackburnQueryKind.OLDER_OPEN and row.decision:
                    continue
                if row.public_reference in seen:
                    continue
                seen.add(row.public_reference)
                seen_order.append(row.public_reference)
                locator = BlackburnLocatorV1(
                    record_id=row.record_id,
                    public_reference=row.public_reference,
                )
                fresh.append(
                    SourceReference(
                        source_id=SOURCE,
                        reference=row.public_reference,
                        locator=locator.model_dump_json(),
                    )
                )
            pending = progress.pending_queries[1:]
            progress = progress.model_copy(
                update={
                    "pending_queries": pending,
                    "completed_queries": (*progress.completed_queries, query),
                    "seen_references": tuple(seen_order),
                    "live_complete": not pending,
                }
            )
            yield DiscoveryBatch(
                references=tuple(fresh),
                next_checkpoint=progress,
                complete=progress.live_complete,
            )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BlackburnWithDarwenApplicationV2]:
        """Read one fixture or live public application without attachments."""
        if session.mode != TransportMode.FIXTURE:
            return await self._fetch_live(session, reference)
        return await self._fetch_fixture(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BlackburnWithDarwenApplicationV2]:
        """Preserve the deterministic fixture contract."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = BlackburnWithDarwenApplicationV2(
            record_id=_required_fixture(
                html,
                r'data-blackburn-key="([^"]+)"',
                "explorer key",
            ),
            council_reference=reference.reference,
            development_proposal=unescape(
                _required_fixture(
                    html,
                    r'data-blackburn-proposal="([^"]+)"',
                    "proposal",
                )
            ),
            public_status=_required_fixture(
                html,
                r'data-blackburn-status="([^"]+)"',
                "status",
            ),
            ward=_required_fixture(
                html,
                r'data-blackburn-area="([^"]+)"',
                "planning area",
            ),
        )
        unavailable = UnavailableSection(reason="live detail is not implemented")
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
    ) -> NativeSnapshot[BlackburnWithDarwenApplicationV2]:
        """Parse the rendered Citizen detail page and document metadata."""
        if not isinstance(session, BlackburnPageSession):
            raise BlackburnBrowserSessionRequiredError
        locator = _parse_locator(reference)
        detail = await session.application(locator)
        record_id, fields = _parse_application_details(detail.body)
        if record_id != locator.record_id:
            raise BlackburnRecordIdMismatchError(locator.record_id, record_id)
        published = _required_mapping(fields, "application reference number")
        if published != reference.reference:
            raise BlackburnReferenceMismatchError(reference.reference, published)
        documents = _parse_documents(detail.body)
        payload = BlackburnWithDarwenApplicationV2(
            record_id=record_id,
            council_reference=published,
            development_proposal=_required_mapping(fields, "proposal"),
            public_status=_required_mapping(fields, "application status"),
            application_type=_required_mapping(fields, "application type"),
            applicant=_optional_mapping(fields, "applicant"),
            agent=_optional_mapping(fields, "agent"),
            location=_required_mapping(fields, "location"),
            grid_reference=_optional_mapping(fields, "grid reference"),
            ward=_optional_mapping(fields, "ward"),
            community=_optional_mapping(fields, "parish / community"),
            officer=_optional_mapping(fields, "officer"),
            decision_level=_optional_mapping(fields, "decision level"),
            received_date=_optional_date(fields, "received date"),
            valid_date=_optional_date(fields, "valid date"),
            expiry_date=_optional_date(fields, "expiry date"),
            decision_date=_optional_date(fields, "decision issued date"),
            decision=_optional_mapping(fields, "decision"),
            documents=documents,
        )
        comments = UnavailableSection(
            reason="the public Citizen portal exposes no application comments section"
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=comments,
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[BlackburnWithDarwenApplicationV2],
    ) -> NormalisedObservation:
        """Map Blackburn application and document metadata to common fields."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.development_proposal,
            status=payload.public_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=document.title, url=document.source_url)
                for document in payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="blackburn-with-darwen-v2",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                decision=payload.decision,
                address=payload.location,
                received_date=payload.received_date,
                validated_date=payload.valid_date,
                decision_date=payload.decision_date,
                source_url=snapshot.evidence[0].url,
                published_parties=tuple(
                    party
                    for party in (payload.applicant, payload.agent)
                    if party is not None
                ),
                officer_name=payload.officer,
            ),
        )


def _initial_queries(window: DiscoveryWindow) -> tuple[BlackburnQueryV1, ...]:
    recent = BlackburnDateRangeV1(start=window.start, end=window.end)
    queries = [
        BlackburnQueryV1(kind=BlackburnQueryKind.RECEIVED, date_range=recent),
        BlackburnQueryV1(kind=BlackburnQueryKind.VALID, date_range=recent),
        BlackburnQueryV1(kind=BlackburnQueryKind.DECISION, date_range=recent),
    ]
    if window.include_open and window.start > _HISTORICAL_START:
        queries.append(
            BlackburnQueryV1(
                kind=BlackburnQueryKind.OLDER_OPEN,
                date_range=BlackburnDateRangeV1(
                    start=_HISTORICAL_START,
                    end=window.start - timedelta(days=1),
                ),
            )
        )
    return tuple(queries)


def _split_query(query: BlackburnQueryV1) -> tuple[BlackburnQueryV1, ...]:
    date_range = query.date_range
    if date_range.start == date_range.end:
        raise BlackburnResultCapError(query)
    midpoint = date_range.start + (date_range.end - date_range.start) // 2
    return (
        query.model_copy(
            update={
                "date_range": BlackburnDateRangeV1(
                    start=date_range.start,
                    end=midpoint,
                )
            }
        ),
        query.model_copy(
            update={
                "date_range": BlackburnDateRangeV1(
                    start=midpoint + timedelta(days=1),
                    end=date_range.end,
                )
            }
        ),
    )


def _parse_search_rows(body: bytes) -> tuple[BlackburnSearchRowV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    table = _search_table(soup)
    if table is None:
        return ()
    headers = _search_headers(table)
    rows = tuple(_parse_search_row(row, headers) for row in table.select("tbody tr"))
    references = tuple(row.public_reference for row in rows)
    if len(references) != len(set(references)):
        duplicate = next(
            reference
            for index, reference in enumerate(references)
            if reference in references[:index]
        )
        raise BlackburnDuplicateReferenceError(duplicate)
    if len(rows) > _RESULT_CAP:
        raise BlackburnResultCountError(len(rows))
    return rows


def _search_table(soup: BeautifulSoup) -> Tag | None:
    tables = soup.select("table#application_results_table")
    if not tables:
        if "no results found." in _normalise(soup.get_text(" ", strip=True)):
            return None
        return _raise_parse("search result table or explicit empty marker")
    if len(tables) != 1:
        return _raise_parse("single search result table")
    return tables[0]


def _search_headers(table: Tag) -> tuple[str, ...]:
    headers = tuple(
        _normalise(header.get_text(" ", strip=True))
        for header in table.select("thead th")
    )
    if headers != _RESULT_HEADERS:
        return _raise_parse("search result headers")
    return headers


def _parse_search_row(
    row: Tag,
    headers: tuple[str, ...],
) -> BlackburnSearchRowV1:
    cells = row.find_all("td", recursive=False)
    if len(cells) != len(headers):
        return _raise_parse("search result columns")
    values = {
        header: unescape(cell.get_text(" ", strip=True))
        for header, cell in zip(headers, cells, strict=True)
    }
    view_buttons = row.select("button.view_application[data-id]")
    if len(view_buttons) != 1:
        return _raise_parse("search result View locator")
    record_id = str(view_buttons[0].get("data-id", "")).strip()
    if not record_id.isdigit():
        return _raise_parse("numeric search result View locator")
    return BlackburnSearchRowV1(
        public_reference=_required_mapping(values, "application reference"),
        record_id=record_id,
        application_type=_required_mapping(values, "application type"),
        location=_required_mapping(values, "location details"),
        proposal=_required_mapping(values, "proposal"),
        ward=_optional_mapping(values, "ward"),
        community=_optional_mapping(values, "community"),
        decision=_optional_mapping(values, "decision"),
    )


def _parse_locator(reference: SourceReference) -> BlackburnLocatorV1:
    if reference.source_id != SOURCE or reference.locator is None:
        raise BlackburnRoutingError(reference.reference)
    try:
        locator = BlackburnLocatorV1.model_validate_json(reference.locator)
    except ValueError as error:
        raise BlackburnRoutingError(reference.reference) from error
    if locator.public_reference != reference.reference:
        raise BlackburnRoutingError(reference.reference)
    return locator


def _parse_application_details(body: bytes) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(body, "html.parser")
    containers = soup.select("#application_details[data-application-id]")
    if len(containers) != 1:
        return _raise_parse("single application details container")
    container = containers[0]
    record_id = str(container.get("data-application-id", "")).strip()
    if not record_id.isdigit():
        return _raise_parse("numeric application id")
    fields: dict[str, str] = {}
    for row in container.select(".row"):
        labels = row.select("strong")
        columns = row.find_all("div", recursive=False)
        if len(labels) != 1 or len(columns) != _DETAIL_COLUMN_COUNT:
            return _raise_parse("application detail row")
        label = _normalise(labels[0].get_text(" ", strip=True)).removesuffix(":")
        if label in fields:
            return _raise_parse("unique application detail label")
        fields[label] = unescape(columns[1].get_text(" ", strip=True)).strip()
    for required in (
        "application reference number",
        "application type",
        "proposal",
        "location",
        "application status",
    ):
        _required_mapping(fields, required)
    return record_id, fields


def _parse_documents(body: bytes) -> tuple[BlackburnDocumentV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    tables = soup.select("table#application_documents")
    if len(tables) != 1:
        return _raise_parse("single application documents table")
    table = tables[0]
    headers = tuple(
        str(header.get("data-field-name", "")).strip()
        for header in table.select("thead th")
    )
    expected = (
        "document_type",
        "description",
        "thumbnail",
        "date_document_added",
        "download",
    )
    if headers != expected:
        return _raise_parse("application document headers")
    documents = tuple(
        _parse_document_row(row, expected) for row in table.select("tbody tr")
    )
    urls = tuple(str(document.source_url) for document in documents)
    if len(urls) != len(set(urls)):
        return _raise_parse("unique application document links")
    return documents


def _parse_document_row(
    row: Tag,
    headers: tuple[str, ...],
) -> BlackburnDocumentV1:
    cells = row.find_all("td", recursive=False)
    if len(cells) != len(headers):
        return _raise_parse("application document columns")
    if tuple(str(cell.get("data-field-name", "")) for cell in cells) != headers:
        return _raise_parse("application document field names")
    values = {
        header: unescape(cell.get_text(" ", strip=True))
        for header, cell in zip(headers, cells, strict=True)
    }
    links = cells[-1].select("a[href]")
    if len(links) != 1:
        return _raise_parse("application document link")
    href = str(links[0].get("href", "")).strip()
    if "fa=downloadDocument" not in href or "id=" not in href:
        return _raise_parse("application document metadata URL")
    raw_date = str(cells[-2].get("data-date-value", "")).strip()
    try:
        published_date = date.fromisoformat(raw_date)
    except ValueError:
        return _raise_parse("application document date")
    return BlackburnDocumentV1(
        title=_required_mapping(values, "description"),
        document_type=_required_mapping(values, "document_type"),
        published_date=published_date,
        source_url=HttpUrl(urljoin(f"{BASE_URL}/", href)),
    )


def _optional_date(values: dict[str, str], name: str) -> date | None:
    value = _optional_mapping(values, name)
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").replace(tzinfo=UTC).date()
    except ValueError as error:
        raise BlackburnWithDarwenParseError(name) from error


def _optional_mapping(values: dict[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def _required_mapping(values: dict[str, str], name: str) -> str:
    value = _optional_mapping(values, name)
    if value is None:
        return _raise_parse(name)
    return value


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        return _raise_parse(field)
    return match.group(1).strip()


class BlackburnWithDarwenParseError(ValueError):
    """A required Blackburn with Darwen boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name the safe boundary field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Blackburn with Darwen field {field}")


class BlackburnResultCapError(RuntimeError):
    """One day still reached the unpaginated public result ceiling."""

    def __init__(self, query: BlackburnQueryV1) -> None:
        """Name the ambiguous query without source response content."""
        super().__init__(f"Blackburn result cap is ambiguous for {query.key}")


class BlackburnResultCountError(ValueError):
    """A rendered result exceeded the observed public ceiling."""

    def __init__(self, count: int) -> None:
        """Describe the impossible count."""
        super().__init__(f"Blackburn result count exceeded {_RESULT_CAP}: {count}")


class BlackburnDuplicateReferenceError(ValueError):
    """One result table repeated a public reference."""

    def __init__(self, reference: str) -> None:
        """Name the duplicate public reference."""
        super().__init__(f"Blackburn search repeated reference {reference}")


class BlackburnRoutingError(ValueError):
    """A source reference lacks a valid Citizen portal locator."""

    def __init__(self, reference: str) -> None:
        """Name the public reference whose route cannot be trusted."""
        super().__init__(f"Blackburn route is unavailable for {reference}")


class BlackburnReferenceMismatchError(ValueError):
    """The detail page published a different public reference."""

    def __init__(self, expected: str, observed: str) -> None:
        """Report both public values without source body content."""
        super().__init__(
            f"Blackburn reference mismatch: expected {expected}, observed {observed}"
        )


class BlackburnRecordIdMismatchError(ValueError):
    """The detail page published a different internal record id."""

    def __init__(self, expected: str, observed: str) -> None:
        """Report both numeric identifiers."""
        super().__init__(
            f"Blackburn record id mismatch: expected {expected}, observed {observed}"
        )


class BlackburnBrowserSessionRequiredError(RuntimeError):
    """Live Blackburn collection requires the guarded browser session."""


def _raise_parse(field: str) -> NoReturn:
    raise BlackburnWithDarwenParseError(field)
