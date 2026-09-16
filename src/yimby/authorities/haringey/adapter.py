# Copyright (c) 2026 Kostas Stathoulopoulos

"""Haringey-owned fixture and Salesforce browser adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable
from urllib.parse import quote, urljoin

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
    DiscoveryWindow,
    DocumentRecord,
    EmptySection,
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
from yimby.portal_time import england_calendar_date
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from yimby.transport import PortalSession

SOURCE = SourceId("haringey-salesforce-register")
BASE_URL = "https://londonboroughofharingey.my.site.com/pr/s"
REGISTER_NAME = "Arcus_BE_Public_Register"
QUICK_LINK_NAME = "Planning Applications Validated in last 7 days"
_RECENT_DAYS = 7
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")


class HaringeyCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable Salesforce result-page progress."""

    page_token: str = "first"  # noqa: S105 - fixture cursor, not a credential.
    window_start: date | None = None
    window_end: date | None = None
    next_page: int = Field(default=1, ge=1)
    reported_page_count: int | None = Field(default=None, ge=1)
    reported_result_count: int | None = Field(default=None, ge=0)
    observed_result_count: int = Field(default=0, ge=0)
    seen_references: tuple[str, ...] = ()
    quick_link_complete: bool = False
    older_open_complete: bool = False


class HaringeySearchHitV1(FrozenModel):
    """One typed row extracted by the Haringey page object."""

    record_id: str = Field(min_length=1)
    public_reference: str = Field(min_length=1)
    address: str = Field(min_length=1)
    proposal: str = Field(min_length=1)
    valid_date: date
    status: str = Field(min_length=1)
    detail_url: HttpUrl


class HaringeySearchPageV1(FrozenModel):
    """One rendered quick-link page and its reported totals."""

    register_name: str = Field(min_length=1)
    quick_link_name: str = Field(min_length=1)
    encoded_query: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    reported_page_count: int = Field(ge=1)
    reported_result_count: int = Field(ge=0)
    hits: tuple[HaringeySearchHitV1, ...]
    evidence: EvidenceCapture


class HaringeyApplicationPagesV1(FrozenModel):
    """Rendered detail, comments, and files tabs for one application."""

    detail: EvidenceCapture
    comments: EvidenceCapture
    files: EvidenceCapture
    reported_file_count: int = Field(ge=0)


@runtime_checkable
class HaringeyPageSession(Protocol):
    """Authority-owned semantic browser interaction boundary."""

    async def validated_last_seven_days(self, page_number: int) -> HaringeySearchPageV1:
        """Render one page of the evidenced seven-day quick link."""
        ...

    async def application_pages(
        self, locator: HaringeyLocatorV1
    ) -> HaringeyApplicationPagesV1:
        """Render the three evidenced application tabs without downloads."""
        ...


class HaringeyLocatorV1(FrozenModel):
    """Source-local route retained separately from the public reference."""

    register_name: str
    quick_link_name: str
    encoded_query: str
    result_page: int = Field(ge=1)
    record_id: str = Field(min_length=1)
    public_reference: str = Field(min_length=1)
    detail_url: HttpUrl


class HaringeyFileV1(FrozenModel):
    """Salesforce file metadata without an attachment body."""

    published_date: date
    title: str
    media_type: str
    size: str
    source_url: HttpUrl


class HaringeyApplicationV1(FrozenModel):
    """Haringey-native application data returned by the public widget."""

    case_number: str
    proposal_text: str
    public_stage: str
    salesforce_record_id: str
    register_name: str | None = None
    quick_link_name: str | None = None
    encoded_query: str | None = None
    result_page: int | None = None
    application_type: str | None = None
    address: str | None = None
    officer: str | None = None
    determination_level: str | None = None
    ward: str | None = None
    applicant: str | None = None
    agent: str | None = None
    valid_date: date | None = None
    consultation_end_date: date | None = None
    target_decision_date: date | None = None
    planning_portal_reference: str | None = None
    gis_constraints_url: HttpUrl | None = None
    files: tuple[HaringeyFileV1, ...] = ()
    document_state: SectionState | None = None
    comment_state: SectionState | None = None


class HaringeyAdapter:
    """Own Haringey's page-object vocabulary and reconciliation rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("haringey"),
        name="London Borough of Haringey",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    def __init__(self, today: Callable[[], date] | None = None) -> None:
        """Inject the local date so the rolling window is deterministic."""
        self._today = today or england_calendar_date

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: HaringeyCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[HaringeyCheckpointV1]]:
        """Use fixtures or exhaust the evidenced seven-day quick link."""
        if session.mode == TransportMode.FIXTURE:
            async for batch in self._discover_fixture(session, window, checkpoint):
                yield batch
            return
        if not isinstance(session, HaringeyPageSession):
            raise HaringeyBrowserSessionRequiredError
        async for batch in self._discover_live(session, window, checkpoint):
            yield batch

    async def _discover_fixture(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: HaringeyCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[HaringeyCheckpointV1]]:
        cursor = "first" if checkpoint is None else checkpoint.page_token
        url = (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&page={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-haringey-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(
            html,
            r'data-haringey-page="([^"]+)"',
            "page token",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=HaringeyCheckpointV1(page_token=next_page),
            complete=next_page == "complete",
        )

    async def _discover_live(
        self,
        session: HaringeyPageSession,
        window: DiscoveryWindow,
        checkpoint: HaringeyCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[HaringeyCheckpointV1]]:
        _assert_recent_window(window, self._today())
        progress = checkpoint or HaringeyCheckpointV1(
            window_start=window.start,
            window_end=window.end,
        )
        _assert_checkpoint_window(progress, window)
        if progress.quick_link_complete:
            if window.include_open:
                raise HaringeyOlderOpenUnavailableError
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        while True:
            page = await session.validated_last_seven_days(progress.next_page)
            _validate_page(page, progress)
            seen = set(progress.seen_references)
            references = []
            for hit in page.hits:
                if hit.public_reference in seen:
                    continue
                seen.add(hit.public_reference)
                locator = HaringeyLocatorV1(
                    register_name=page.register_name,
                    quick_link_name=page.quick_link_name,
                    encoded_query=page.encoded_query,
                    result_page=page.page_number,
                    record_id=hit.record_id,
                    public_reference=hit.public_reference,
                    detail_url=hit.detail_url,
                )
                references.append(
                    SourceReference(
                        source_id=SOURCE,
                        reference=hit.public_reference,
                        locator=locator.model_dump_json(),
                    )
                )
            observed = progress.observed_result_count + len(page.hits)
            is_last = page.page_number == page.reported_page_count
            if is_last and observed != page.reported_result_count:
                raise HaringeyResultCountMismatchError(
                    page.reported_result_count, observed
                )
            progress = progress.model_copy(
                update={
                    "page_token": (
                        "complete" if is_last else str(page.page_number + 1)
                    ),
                    "window_start": window.start,
                    "window_end": window.end,
                    "next_page": page.page_number + 1,
                    "reported_page_count": page.reported_page_count,
                    "reported_result_count": page.reported_result_count,
                    "observed_result_count": observed,
                    "seen_references": tuple(seen),
                    "quick_link_complete": is_last,
                }
            )
            yield DiscoveryBatch(
                references=tuple(references),
                next_checkpoint=progress,
                complete=is_last and not window.include_open,
            )
            if is_last:
                if window.include_open:
                    raise HaringeyOlderOpenUnavailableError
                return

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[HaringeyApplicationV1]:
        """Read fixture detail or the three evidenced Salesforce tabs."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        if not isinstance(session, HaringeyPageSession):
            raise HaringeyBrowserSessionRequiredError
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[HaringeyApplicationV1]:
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = HaringeyApplicationV1(
            case_number=reference.reference,
            proposal_text=unescape(
                _required_fixture(html, r'data-haringey-proposal="([^"]+)"', "proposal")
            ),
            public_stage=_required_fixture(
                html,
                r'data-haringey-stage="([^"]+)"',
                "stage",
            ),
            salesforce_record_id=_required_fixture(
                html,
                r'data-haringey-record-id="([^"]+)"',
                "record id",
            ),
        )
        unavailable = UnavailableSection(reason="JavaScript section not verified live")
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
        session: HaringeyPageSession,
        reference: SourceReference,
    ) -> NativeSnapshot[HaringeyApplicationV1]:
        locator = _parse_locator(reference)
        pages = await session.application_pages(locator)
        fields, constraints_url = _parse_detail(pages.detail.body)
        published = _required_field(fields, "reference")
        if published != reference.reference:
            raise HaringeyReferenceMismatchError(reference.reference, published)
        files, document_state = _files_or_failure(
            pages.files.body, pages.reported_file_count
        )
        comment_state = _comments_or_failure(pages.comments.body)
        payload = HaringeyApplicationV1(
            case_number=published,
            proposal_text=_required_field(fields, "proposal"),
            public_stage=_required_field(fields, "status"),
            salesforce_record_id=locator.record_id,
            register_name=locator.register_name,
            quick_link_name=locator.quick_link_name,
            encoded_query=locator.encoded_query,
            result_page=locator.result_page,
            application_type=_required_field(fields, "application type"),
            address=_required_field(fields, "address"),
            officer=_required_field(fields, "officer"),
            determination_level=_required_field(fields, "determination level"),
            ward=_required_field(fields, "ward"),
            applicant=_required_field(fields, "applicant"),
            agent=_required_field(fields, "agent"),
            valid_date=_required_date(fields, "valid date"),
            consultation_end_date=_required_date(fields, "consultation end date"),
            target_decision_date=_required_date(fields, "target decision date"),
            planning_portal_reference=_required_field(
                fields, "planning portal reference"
            ),
            gis_constraints_url=constraints_url,
            files=files,
            document_state=document_state,
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
            evidence=(pages.detail, pages.comments, pages.files),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[HaringeyApplicationV1],
    ) -> NormalisedObservation:
        """Map Haringey stage, summary, and file metadata to common fields."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_text,
            status=payload.public_stage.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.source_url)
                for item in payload.files
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="haringey-v2",
            metadata=ApplicationMetadata(
                aliases=(
                    ()
                    if payload.planning_portal_reference is None
                    else (payload.planning_portal_reference,)
                ),
                application_type=payload.application_type,
                address=payload.address,
                validated_date=payload.valid_date,
                published_parties=tuple(
                    party
                    for party in (payload.applicant, payload.agent)
                    if party is not None
                ),
                officer_name=payload.officer,
            ),
        )


def _assert_recent_window(window: DiscoveryWindow, today: date) -> None:
    if window.start != today - timedelta(days=_RECENT_DAYS - 1) or window.end != today:
        raise HaringeyWindowUnavailableError(window.start, window.end)


def _assert_checkpoint_window(
    checkpoint: HaringeyCheckpointV1, window: DiscoveryWindow
) -> None:
    if checkpoint.window_start is not None and (
        checkpoint.window_start != window.start or checkpoint.window_end != window.end
    ):
        raise HaringeyCheckpointError


def _validate_page(
    page: HaringeySearchPageV1, checkpoint: HaringeyCheckpointV1
) -> None:
    if page.page_number != checkpoint.next_page:
        raise HaringeyPageMismatchError(checkpoint.next_page, page.page_number)
    if page.page_number < page.reported_page_count and not page.hits:
        raise HaringeyEmptyIntermediatePageError(page.page_number)
    if checkpoint.reported_page_count is not None and (
        page.reported_page_count != checkpoint.reported_page_count
        or page.reported_result_count != checkpoint.reported_result_count
    ):
        raise HaringeyReportedTotalsChangedError


def _parse_locator(reference: SourceReference) -> HaringeyLocatorV1:
    if reference.source_id != SOURCE or reference.locator is None:
        raise HaringeyRoutingError(reference.reference)
    try:
        return HaringeyLocatorV1.model_validate_json(reference.locator)
    except ValueError as error:
        raise HaringeyRoutingError(reference.reference) from error


def _parse_detail(body: bytes) -> tuple[dict[str, str], HttpUrl]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for term in soup.select("dt"):
        sibling = term.find_next_sibling("dd")
        if sibling is None:
            continue
        fields[_normalise(term.get_text(" ", strip=True))] = unescape(
            sibling.get_text(" ", strip=True)
        )
    for required in (
        "reference",
        "application type",
        "address",
        "proposal",
        "status",
        "officer",
        "determination level",
        "ward",
        "applicant",
        "agent",
        "valid date",
        "consultation end date",
        "target decision date",
        "planning portal reference",
    ):
        _required_field(fields, required)
    constraint = next(
        (
            link
            for link in soup.select("a[href]")
            if "constraint" in _normalise(link.get_text(" ", strip=True))
        ),
        None,
    )
    if not isinstance(constraint, Tag):
        _raise_parse("GIS constraints link")
    return fields, HttpUrl(urljoin(f"{BASE_URL}/", str(constraint.get("href", ""))))


def _comments_or_failure(body: bytes) -> SectionState:
    try:
        text = _normalise(BeautifulSoup(body, "html.parser").get_text(" ", strip=True))
        if "there are no comments." not in text:
            _raise_parse("explicit empty current comments")
    except HaringeyParseError as error:
        return FailedSection(code=error.code)
    return EmptySection()


def _files_or_failure(
    body: bytes, reported_count: int
) -> tuple[tuple[HaringeyFileV1, ...], SectionState]:
    try:
        files = _parse_files(body)
        _assert_file_count(reported_count, len(files))
    except (HaringeyParseError, HaringeyFileCountMismatchError) as error:
        return (), FailedSection(code=error.code)
    return files, collection_state(len(files))


def _assert_file_count(expected: int, observed: int) -> None:
    if observed != expected:
        raise HaringeyFileCountMismatchError(expected, observed)


def _parse_files(body: bytes) -> tuple[HaringeyFileV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    for table in soup.select("table"):
        headers = tuple(
            _normalise(header.get_text(" ", strip=True))
            for header in table.select("thead th")
        )
        if not {"date", "title", "download"}.issubset(headers) and not {
            "date",
            "title",
            "type",
            "size",
        }.issubset(headers):
            continue
        files = []
        for row in table.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != len(headers):
                _raise_parse("file row columns")
            values = {
                header: cell.get_text(" ", strip=True)
                for header, cell in zip(headers, cells, strict=True)
            }
            links = [
                link
                for link in row.select("a[href]")
                if "download all" not in _normalise(link.get_text(" ", strip=True))
            ]
            if len(links) != 1:
                _raise_parse("file source link")
            descriptor = " ".join(
                value
                for value in (
                    str(links[0].get("aria-label", "")),
                    str(links[0].get("title", "")),
                    links[0].get_text(" ", strip=True),
                )
                if value
            )
            media_type = values.get("type") or _file_media_type(descriptor)
            size = values.get("size") or _file_size(descriptor)
            files.append(
                HaringeyFileV1(
                    published_date=_parse_date(_required_field(values, "date")),
                    title=_required_field(values, "title"),
                    media_type=media_type,
                    size=size,
                    source_url=HttpUrl(
                        urljoin(f"{BASE_URL}/", str(links[0].get("href", "")))
                    ),
                )
            )
        return tuple(files)
    return _raise_parse("files metadata table")


def _required_date(fields: dict[str, str], name: str) -> date:
    value = _required_field(fields, name)
    try:
        return _parse_date(value)
    except ValueError as error:
        raise HaringeyParseError(name) from error


def _parse_date(value: str) -> date:
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise ValueError(value)


def _file_media_type(descriptor: str) -> str:
    match = re.search(
        r"\b(PDF|DOCX?|XLSX?|CSV|JPE?G|PNG|TIFF?)\b",
        descriptor,
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse("file media type")
    return match.group(1).upper()


def _file_size(descriptor: str) -> str:
    match = re.search(r"\b[\d,.]+\s*(?:bytes?|[KMGT]B)\b", descriptor, re.IGNORECASE)
    if match is None:
        _raise_parse("file size")
    return match.group(0)


def _required_field(fields: dict[str, str], name: str) -> str:
    value = fields.get(name)
    if not value:
        _raise_parse(name)
    return value


def _normalise(value: str) -> str:
    return " ".join(value.strip().rstrip(":").casefold().split())


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class HaringeyParseError(ValueError):
    """A required Haringey boundary field was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe field rather than response content."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Haringey field {field}")


class HaringeyBrowserSessionRequiredError(RuntimeError):
    """The live Haringey boundary requires its semantic page object."""


class HaringeyWindowUnavailableError(RuntimeError):
    """Only the rolling seven-day quick link is evidenced."""

    def __init__(self, start: date, end: date) -> None:
        """Describe the unsupported interval without inventing a search."""
        super().__init__(
            f"Haringey only supports the current seven-day window: {start}/{end}"
        )


class HaringeyCheckpointError(ValueError):
    """A checkpoint belongs to another seven-day interval."""


class HaringeyPageMismatchError(ValueError):
    """The page object returned a page other than the requested page."""

    def __init__(self, expected: int, observed: int) -> None:
        """Record safe page numbers only."""
        super().__init__(f"expected Haringey page {expected}, observed {observed}")


class HaringeyEmptyIntermediatePageError(ValueError):
    """Pagination stopped before its reported final page."""

    def __init__(self, page: int) -> None:
        """Identify the unexpected empty page."""
        super().__init__(f"Haringey page {page} was empty before the final page")


class HaringeyReportedTotalsChangedError(ValueError):
    """Reported page or result counts changed during one traversal."""


class HaringeyResultCountMismatchError(ValueError):
    """Observed quick-link rows did not reconcile to the reported total."""

    def __init__(self, expected: int, observed: int) -> None:
        """Expose only aggregate counts."""
        super().__init__(f"Haringey reported {expected} results, observed {observed}")


class HaringeyOlderOpenUnavailableError(RuntimeError):
    """Older-open enumeration has not been evidenced."""


class HaringeyRoutingError(ValueError):
    """A reference lacks a valid Salesforce locator."""

    def __init__(self, reference: str) -> None:
        """Identify the public reference only."""
        super().__init__(f"Haringey cannot route reference {reference}")


class HaringeyReferenceMismatchError(ValueError):
    """The rendered detail belongs to another public reference."""

    def __init__(self, expected: str, observed: str) -> None:
        """Record both public references."""
        super().__init__(f"expected Haringey reference {expected}, observed {observed}")


class HaringeyFileCountMismatchError(ValueError):
    """Rendered file rows did not match the page object's count."""

    def __init__(self, expected: int, observed: int) -> None:
        """Expose only aggregate counts."""
        self.code = "file-count-mismatch"
        super().__init__(f"Haringey reported {expected} files, observed {observed}")


def _raise_parse(field: str) -> NoReturn:
    raise HaringeyParseError(field)
