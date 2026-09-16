# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden-owned discovery, extraction, and normalisation."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

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
    FailedSection,
    FrozenModel,
    NativeComment,
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
from yimby.geo import bng_to_wgs84
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

SEARCH_SOURCE = SourceId("camden-jsf-search")
SEARCH_BASE = "https://accountforms.camden.gov.uk/planning-search"
DETAIL_BASE = "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer"
DOCUMENT_BASE = "https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec"
_REDIRECT_BASE = (
    "https://planningrecords.camden.gov.uk/NECSWS/Redirection/redirect.aspx"
)
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")
_MINIMUM_LABELLED_CELLS = 2


class CamdenCheckpointV1(FrozenModel):
    """Camden fixture cursor; bounded live enumeration remains unavailable."""

    view_state_page: str


class CamdenDocumentV1(FrozenModel):
    """Camden document-service metadata without attachment content."""

    title: str
    url: HttpUrl
    created_date: date | None = None
    document_type: str | None = None


class CamdenApplicationV1(FrozenModel):
    """Camden-native record spanning Northgate and CMWebDrawer."""

    public_reference: str
    proposal: str
    current_status: str
    grid_easting: int
    grid_northing: int
    documents: tuple[CamdenDocumentV1, ...]
    comments: tuple[NativeComment, ...]
    northgate_key: str | None = None
    address: str | None = None
    application_type: str | None = None
    development_type: str | None = None
    ward: str | None = None
    case_officer: str | None = None
    published_parties: tuple[str, ...] = ()
    document_state: SectionState | None = None


class CamdenAdapter:
    """Own Camden's three-service request and completeness rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("camden"),
        name="London Borough of Camden",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(
            SourceDefinition(
                id=SEARCH_SOURCE,
                base_url=HttpUrl(f"{SEARCH_BASE}/"),
                valid_from=date(2010, 1, 1),
            ),
            SourceDefinition(
                id=SourceId("camden-northgate-records"),
                base_url=HttpUrl(f"{DETAIL_BASE}/"),
            ),
            SourceDefinition(
                id=SourceId("camden-cmwebdrawer-documents"),
                base_url=HttpUrl(DOCUMENT_BASE),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CamdenCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CamdenCheckpointV1]]:
        """Preserve fixtures and reject unproven live date enumeration."""
        if session.mode != TransportMode.FIXTURE:
            raise CamdenBoundedDiscoveryUnavailableError
        cursor = "initial" if checkpoint is None else checkpoint.view_state_page
        url = (
            f"{SEARCH_BASE}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&view={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SEARCH_SOURCE, reference=value)
            for value in re.findall(r'data-camden-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(html, r'data-camden-next="([^"]+)"', "next page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=CamdenCheckpointV1(view_state_page=next_page),
            complete=next_page == "complete",
        )

    async def resolve_exact(
        self, session: PortalSession, public_reference: str
    ) -> SourceReference:
        """Resolve one explicit public reference to its Northgate numeric key."""
        if session.mode == TransportMode.FIXTURE:
            raise CamdenExactSearchLiveOnlyError
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(f"{SEARCH_BASE}/"), intent=RequestIntent.SEARCH)
        )
        form = _parse_jsf_form(form_capture.body)
        result = await session.fetch(_exact_search_request(form, public_reference))
        return _parse_exact_result(result.body, public_reference)

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CamdenApplicationV1]:
        """Fetch fixture detail or live Northgate and CMWebDrawer records."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CamdenApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{DETAIL_BASE}/application?reference={encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        documents = tuple(
            CamdenDocumentV1(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-camden-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = CamdenApplicationV1(
            public_reference=reference.reference,
            proposal=unescape(
                _required_fixture(html, r'data-camden-proposal="([^"]+)"', "proposal")
            ),
            current_status=_required_fixture(
                html, r'data-camden-status="([^"]+)"', "status"
            ),
            grid_easting=int(
                _required_fixture(html, r'data-camden-easting="([^"]+)"', "easting")
            ),
            grid_northing=int(
                _required_fixture(html, r'data-camden-northing="([^"]+)"', "northing")
            ),
            documents=documents,
            comments=(),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=UnavailableSection(reason="comment enumeration not verified"),
            ),
            evidence=(detail,),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CamdenApplicationV1]:
        if reference.source_id != SEARCH_SOURCE or reference.locator is None:
            raise CamdenRoutingError(reference.reference)
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(_redirect_url(reference.locator)),
                intent=RequestIntent.DETAIL,
            )
        )
        fields = _parse_dataview(detail.body)
        published = _required_field(fields, "reference", "application reference")
        if published != reference.reference:
            raise CamdenReferenceMismatchError(reference.reference, published)
        evidence: list[EvidenceCapture] = [detail]
        documents, document_state = await _fetch_documents(
            session, reference.reference, evidence
        )
        payload = CamdenApplicationV1(
            public_reference=published,
            proposal=_required_field(fields, "proposal", "description"),
            current_status=_required_field(fields, "current status", "status"),
            grid_easting=_required_integer(fields, "easting", "bng easting"),
            grid_northing=_required_integer(fields, "northing", "bng northing"),
            documents=documents,
            comments=(),
            northgate_key=reference.locator,
            address=_optional_field(fields, "address", "site address"),
            application_type=_optional_field(fields, "application type"),
            development_type=_optional_field(fields, "development type"),
            ward=_optional_field(fields, "ward"),
            case_officer=_optional_field(fields, "case officer"),
            published_parties=tuple(
                value
                for value in (
                    _optional_field(fields, "applicant"),
                    _optional_field(fields, "agent"),
                )
                if value
            ),
            document_state=document_state,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=document_state,
                comments=UnavailableSection(
                    reason="Camden public comment enumeration is unresolved"
                ),
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[CamdenApplicationV1],
    ) -> NormalisedObservation:
        """Map Camden-native names while preserving native coordinates."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal,
            status=payload.current_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in payload.documents
            ),
            comments=tuple(
                CommentRecord(comment_id=item.comment_id, text=item.text)
                for item in payload.comments
            ),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="camden-v2",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                address=payload.address,
                location=bng_to_wgs84(payload.grid_easting, payload.grid_northing),
                published_parties=payload.published_parties,
                officer_name=payload.case_officer,
                source_url=snapshot.evidence[0].url,
            ),
        )


def _parse_jsf_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form#searchForm") or soup.select_one("form")
    if not isinstance(form, Tag):
        _raise_parse("JSF search form")
    if not form.select_one('input[name="javax.faces.ViewState"][value]'):
        _raise_parse("javax.faces.ViewState")
    return form


def _exact_search_request(form: Tag, reference: str) -> PortalRequest:
    values = {
        "searchForm": "searchForm",
        "searchForm:searchTermInput:textField": reference,
        "searchForm:SubmitButton:button": "Search",
    }
    fields = []
    replaced = set()
    for item in form.select("input[name]"):
        name = str(item.get("name"))
        if name in values:
            fields.append(FormField(name=name, value=values[name]))
            replaced.add(name)
        elif name == "javax.faces.ViewState":
            fields.append(FormField(name=name, value=str(item.get("value", ""))))
    fields.extend(
        FormField(name=name, value=value)
        for name, value in values.items()
        if name not in replaced
    )
    return PortalRequest(
        url=HttpUrl(urljoin(f"{SEARCH_BASE}/", str(form.get("action", "index.xhtml")))),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=tuple(fields),
    )


def _parse_exact_result(body: bytes, expected: str) -> SourceReference:
    soup = BeautifulSoup(body, "html.parser")
    matches = []
    for link in soup.select('a[href*="Redirection/redirect.aspx"]'):
        container = link.find_parent(["tr", "li", "article", "div"])
        text = link.get_text(" ", strip=True)
        if isinstance(container, Tag):
            text = container.get_text(" ", strip=True)
        if expected not in text:
            continue
        locators = parse_qs(urlsplit(str(link.get("href", ""))).query).get("PARAM0", [])
        if len(locators) != 1 or not locators[0]:
            _raise_parse("Northgate numeric key")
        matches.append(locators[0])
    if len(matches) != 1:
        raise CamdenExactSearchMismatchError(expected, len(matches))
    return SourceReference(
        source_id=SEARCH_SOURCE, reference=expected, locator=matches[0]
    )


def _redirect_url(locator: str) -> str:
    return f"{_REDIRECT_BASE}?{urlencode({'linkid': 'EXDC', 'PARAM0': locator})}"


def _document_url(reference: str) -> str:
    return f"{DOCUMENT_BASE}?{urlencode({'q': f'recContainer:"{reference}"'})}"


async def _fetch_documents(
    session: PortalSession,
    reference: str,
    evidence: list[EvidenceCapture],
) -> tuple[tuple[CamdenDocumentV1, ...], SectionState]:
    try:
        capture = await session.fetch(
            PortalRequest(
                url=HttpUrl(_document_url(reference)), intent=RequestIntent.DETAIL
            )
        )
    except SourceUnavailableError:
        return (), FailedSection(code="source-unavailable")
    evidence.append(capture)
    try:
        documents = _parse_documents(capture.body)
    except (CamdenParseError, CamdenDocumentCountMismatchError) as error:
        return (), FailedSection(code=error.code)
    return documents, collection_state(len(documents))


def _parse_documents(body: bytes) -> tuple[CamdenDocumentV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    reported = _reported_document_count(soup)
    documents = []
    for row in soup.select("[data-document-row], tbody tr"):
        link = row.select_one("a[href]")
        if not isinstance(link, Tag):
            continue
        values = _row_values(row)
        title = _mapping_value(values, "title", "description") or link.get_text(
            " ", strip=True
        )
        if not title:
            _raise_parse("document title")
        documents.append(
            CamdenDocumentV1(
                title=title,
                url=HttpUrl(urljoin(f"{DOCUMENT_BASE}/", str(link.get("href", "")))),
                created_date=_parse_date(
                    _mapping_value(values, "created date", "date created", "date")
                ),
                document_type=_mapping_value(values, "document type", "type"),
            )
        )
    if len(documents) != reported:
        raise CamdenDocumentCountMismatchError(reported, len(documents))
    return tuple(documents)


def _reported_document_count(soup: BeautifulSoup) -> int:
    element = soup.select_one("[data-result-count]")
    if isinstance(element, Tag):
        return int(str(element.get("data-result-count")))
    match = re.search(
        r"(?:reported|found|total)\s+(\d+)\s+(?:documents?|records?)",
        soup.get_text(" ", strip=True),
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse("document result count")
    return int(match.group(1))


def _row_values(row: Tag) -> dict[str, str]:
    table = row.find_parent("table")
    headers = (
        ()
        if not isinstance(table, Tag)
        else tuple(
            _normalise_label(item.get_text(" ", strip=True))
            for item in table.select("thead th")
        )
    )
    cells = row.find_all("td", recursive=False)
    if headers and len(headers) == len(cells):
        return {
            header: cell.get_text(" ", strip=True)
            for header, cell in zip(headers, cells, strict=True)
        }
    return {}


def _parse_dataview(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    view = soup.select_one(".dataview")
    if not isinstance(view, Tag):
        _raise_parse("Northgate dataview")
    fields: dict[str, str] = {}
    for row in view.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= _MINIMUM_LABELLED_CELLS:
            fields[_normalise_label(cells[0].get_text(" ", strip=True))] = cells[
                -1
            ].get_text(" ", strip=True)
    for term in view.select("dt"):
        value = term.find_next_sibling("dd")
        if isinstance(value, Tag):
            fields[_normalise_label(term.get_text(" ", strip=True))] = value.get_text(
                " ", strip=True
            )
    if not fields:
        _raise_parse("Northgate labelled values")
    return fields


def _normalise_label(value: str) -> str:
    return " ".join(value.strip().rstrip(":").casefold().split())


def _mapping_value(values: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(_normalise_label(name))
        if value:
            return value
    return None


def _optional_field(fields: dict[str, str], *names: str) -> str | None:
    value = _mapping_value(fields, *names)
    return None if value is None else unescape(value)


def _required_field(fields: dict[str, str], *names: str) -> str:
    value = _optional_field(fields, *names)
    if value is None:
        _raise_parse(f"detail {'/'.join(names)}")
    return value


def _required_integer(fields: dict[str, str], *names: str) -> int:
    value = _required_field(fields, *names)
    try:
        return int(value)
    except ValueError:
        _raise_parse(f"integer {'/'.join(names)}")


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return _raise_parse("document date")


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class CamdenParseError(ValueError):
    """A required Camden boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Camden field {field}")


class CamdenBoundedDiscoveryUnavailableError(RuntimeError):
    """The captured search cannot enumerate a complete date interval."""

    def __init__(self) -> None:
        """Distinguish unsupported discovery from an empty result."""
        super().__init__("Camden bounded live discovery is not verified")


class CamdenExactSearchLiveOnlyError(ValueError):
    """Exact-reference resolution is a live-only operation."""


class CamdenExactSearchMismatchError(ValueError):
    """Exact-reference search did not expose one matching record."""

    def __init__(self, reference: str, matches: int) -> None:
        """Report only the public reference and match count."""
        super().__init__(
            f"Camden exact search for {reference} returned {matches} matches"
        )


class CamdenDocumentCountMismatchError(ValueError):
    """CMWebDrawer's count did not match parsed rows."""

    def __init__(self, expected: int, actual: int) -> None:
        """Report only counts."""
        self.code = "document-count-mismatch"
        super().__init__(f"Camden reported {expected} documents but exposed {actual}")


class CamdenRoutingError(ValueError):
    """A reference lacks Camden's Northgate numeric key."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Camden cannot route reference {reference}")


class CamdenReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(f"expected Camden reference {expected}, received {actual}")


def _raise_parse(field: str) -> NoReturn:
    raise CamdenParseError(field)
