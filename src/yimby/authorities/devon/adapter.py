# Copyright (c) 2026 Kostas Stathoulopoulos

"""Devon-owned fixture and live county planning-register adapter."""

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
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    ExcludedSection,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    TransportMode,
    collection_state,
)
from yimby.transport import FormField, PortalRequest, RequestIntent, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalSession

SOURCE = SourceId("devon-custom-register")
BASE_URL = "https://planning.devon.gov.uk"
_SEARCH_URL = f"{BASE_URL}/Search/Standard?searchType=Received&days=90"
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")


class DevonCheckpointV1(FrozenModel):
    """Fixture cursor plus Devon rolling-search completion."""

    result_page: str
    window_start: date | None = None
    window_end: date | None = None
    live_complete: bool = False


class DevonDocumentV1(FrozenModel):
    """Devon document metadata retained without the attachment body."""

    title: str
    url: HttpUrl
    record_number: str | None = None
    plan_identifier: str | None = None
    image_identifier: str | None = None
    filename: str | None = None


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
    decision_date: date | None = None
    district: str | None = None
    electoral_division: str | None = None
    parish: str | None = None
    applicant: str | None = None
    agent: str | None = None


class DevonAdapter:
    """Own Devon disclaimer, rolling-window, and hidden-tab semantics."""

    manifest = AuthorityManifest(
        id=AuthorityId("devon"),
        name="Devon County Council",
        kind=AuthorityKind.COUNTY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    def __init__(self, today: Callable[[], date] = date.today) -> None:
        """Inject today's date so the rolling 90-day boundary is testable."""
        self._today = today

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DevonCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DevonCheckpointV1]]:
        """Use fixtures or Devon's exact rolling 90-day received search."""
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
        expected_end = self._today()
        expected_start = expected_end - timedelta(days=89)
        if window.start != expected_start or window.end != expected_end:
            raise DevonWindowUnsupportedError(expected_start, expected_end)
        progress = checkpoint or DevonCheckpointV1(result_page="live")
        _assert_window(progress, window)
        if progress.live_complete:
            if window.include_open:
                raise DevonOpenEnumerationUnsupportedError
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        captures = await _fetch_protected(
            session,
            PortalRequest(url=HttpUrl(_SEARCH_URL), intent=RequestIntent.SEARCH),
        )
        references = _parse_search_results(captures[-1].body)
        completed = progress.model_copy(
            update={
                "window_start": window.start,
                "window_end": window.end,
                "live_complete": not window.include_open,
            }
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=completed,
            complete=completed.live_complete,
        )
        if window.include_open:
            raise DevonOpenEnumerationUnsupportedError

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
        return _snapshot(reference, payload, (detail,))

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
        documents = _parse_documents(detail.body)
        payload = DevonApplicationV1(
            council_reference=published,
            application_type=_required_field(fields, "application type", "type"),
            proposal_description=_required_field(fields, "proposal", "description"),
            public_status=_required_field(fields, "status"),
            site_location=_required_field(fields, "location", "site location"),
            documents=documents,
            case_officer=_optional_field(fields, "case officer"),
            received_date=_optional_date(fields, "received date", "date received"),
            validated_date=_optional_date(fields, "validation date", "validated date"),
            decision_date=_optional_date(fields, "decision date"),
            district=_optional_field(fields, "district"),
            electoral_division=_optional_field(fields, "electoral division"),
            parish=_optional_field(fields, "parish"),
            applicant=_optional_field(fields, "applicant"),
            agent=_optional_field(fields, "agent"),
        )
        return _snapshot(reference, payload, captures)

    def normalise(
        self,
        snapshot: NativeSnapshot[DevonApplicationV1],
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
                DocumentRecord(title=item.title, url=item.url)
                for item in payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="devon-v2",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                address=payload.site_location,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                published_parties=tuple(
                    value for value in (payload.applicant, payload.agent) if value
                ),
                officer_name=payload.case_officer,
                source_url=snapshot.evidence[-1].url,
            ),
        )


def _snapshot(
    reference: SourceReference,
    payload: DevonApplicationV1,
    evidence: tuple[EvidenceCapture, ...],
) -> NativeSnapshot[DevonApplicationV1]:
    return NativeSnapshot(
        reference=reference,
        observed_at=datetime.now(UTC),
        payload=payload,
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=collection_state(len(payload.documents)),
            comments=ExcludedSection(
                policy="public responses retained as document metadata only"
            ),
        ),
        evidence=evidence,
    )


async def _fetch_protected(
    session: PortalSession, request: PortalRequest
) -> tuple[EvidenceCapture, ...]:
    first = await session.fetch(request)
    disclaimer = _parse_disclaimer(first.body)
    if disclaimer is None:
        return (first,)
    action, fields = disclaimer
    accepted = await session.fetch(
        PortalRequest(
            url=HttpUrl(urljoin(f"{BASE_URL}/", action)),
            intent=request.intent,
            method=RequestMethod.POST,
            form=fields,
        )
    )
    if _parse_disclaimer(accepted.body) is not None:
        raise DevonDisclaimerAcceptanceError
    return first, accepted


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


def _parse_search_results(body: bytes) -> tuple[SourceReference, ...]:
    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(" ", strip=True)
    if _parse_disclaimer(body) is not None:
        _raise_parse("accepted disclaimer search response")
    if soup.select("a[rel='next'], .pagination"):
        raise DevonSearchCapUnsupportedError
    references = []
    for record in soup.select("dl.searchResultsList"):
        link = record.select_one('a[href*="/Planning/Display/"]')
        if not isinstance(link, Tag):
            _raise_parse("search result detail link")
        href = str(link.get("href", ""))
        value = link.get_text(" ", strip=True)
        if not value:
            value = urlsplit(href).path.partition("/Planning/Display/")[2]
        references.append(
            SourceReference(
                source_id=SOURCE,
                reference=value,
                locator=urljoin(f"{BASE_URL}/", href),
            )
        )
    count = _reported_count(text)
    if count is not None and count != len(references):
        raise DevonCountMismatchError(count, len(references))
    if not references and "no records" not in text.casefold():
        _raise_parse("search results or no-records message")
    return tuple(references)


def _reported_count(text: str) -> int | None:
    match = re.search(
        r"(?:found|showing|total)\s+(\d+)\s+(?:records?|results?)", text, re.IGNORECASE
    )
    return None if match is None else int(match.group(1))


def _parse_labelled_fields(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for block in soup.select("dl.details-grid"):
        for term in block.select("dt"):
            value = term.find_next_sibling("dd")
            if isinstance(value, Tag):
                fields[_normalise_label(term.get_text(" ", strip=True))] = (
                    value.get_text(" ", strip=True)
                )
    if not fields:
        _raise_parse("details-grid")
    return fields


def _parse_documents(body: bytes) -> tuple[DevonDocumentV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    documents = []
    for link in soup.select('a[href*="/Document/Download"]'):
        href = urljoin(f"{BASE_URL}/", str(link.get("href", "")))
        query = parse_qs(urlsplit(href).query)
        title = (
            link.get_text(" ", strip=True)
            or _query_value(query, "filename")
            or "Document"
        )
        documents.append(
            DevonDocumentV1(
                title=title,
                url=HttpUrl(href),
                record_number=_query_value(query, "record", "recordNumber"),
                plan_identifier=_query_value(query, "plan", "planId"),
                image_identifier=_query_value(query, "image", "imageId"),
                filename=_query_value(query, "filename"),
            )
        )
    return tuple(documents)


def _query_value(values: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        found = values.get(name)
        if found and found[0]:
            return found[0]
    return None


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
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return _raise_parse(f"date {'/'.join(names)}")


def _assert_window(checkpoint: DevonCheckpointV1, window: DiscoveryWindow) -> None:
    if checkpoint.window_start is not None and (
        checkpoint.window_start != window.start or checkpoint.window_end != window.end
    ):
        raise DevonCheckpointError


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class DevonParseError(ValueError):
    """A required Devon boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Devon field {field}")


class DevonWindowUnsupportedError(ValueError):
    """The requested dates cannot be proven by the rolling search."""

    def __init__(self, start: date, end: date) -> None:
        """Report the only currently supported live interval."""
        super().__init__(f"Devon live discovery requires {start} through {end}")


class DevonCountMismatchError(ValueError):
    """Devon's displayed count did not match result records."""

    def __init__(self, expected: int, actual: int) -> None:
        """Report only counts."""
        super().__init__(f"Devon reported {expected} results but exposed {actual}")


class DevonSearchCapUnsupportedError(RuntimeError):
    """Unexpected pagination prevents a completeness claim."""


class DevonDisclaimerAcceptanceError(RuntimeError):
    """The protected route remained behind its disclaimer."""


class DevonCheckpointError(ValueError):
    """A saved cursor belongs to another rolling interval."""


class DevonOpenEnumerationUnsupportedError(RuntimeError):
    """Older open applications cannot yet be enumerated completely."""

    def __init__(self) -> None:
        """Keep the unsupported boundary explicit."""
        super().__init__("Devon older-open enumeration is not verified")


class DevonRoutingError(ValueError):
    """A reference lacks Devon's detail locator."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Devon cannot route reference {reference}")


class DevonReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(f"expected Devon reference {expected}, received {actual}")


def _raise_parse(field: str) -> NoReturn:
    raise DevonParseError(field)
