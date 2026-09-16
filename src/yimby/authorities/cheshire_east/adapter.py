# Copyright (c) 2026 Kostas Stathoulopoulos

"""Cheshire East-owned fixture and valid-date search adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl

from yimby.domain import (
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    TransportMode,
    UnavailableSection,
)
from yimby.transport import FormField, PortalRequest, RequestIntent, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("cheshire-east-custom-register")
BASE_URL = "https://pa.cheshireeast.gov.uk/planning"
_SEARCH_URL = f"{BASE_URL}/index.html?fa=search"
_SEARCH_POST_URL = f"{BASE_URL}/index.html"
_WEEKLY_RECEIVED_URL = f"{BASE_URL}/index.html?fa=getReceivedWeeklyList"
_DETAIL_URL = f"{BASE_URL}/index.html?fa=getApplication&id={{locator}}"


class CheshireEastCheckpointV1(FrozenModel):
    """Fixture cursor plus the observed valid-date table boundary."""

    search_page: str
    window_start: date | None = None
    window_end: date | None = None
    seen_references: tuple[str, ...] = ()
    table_observed: bool = False


class CheshireEastApplicationV1(FrozenModel):
    """Cheshire East-native fixture record."""

    public_reference: str
    alternative_reference: str
    development_proposal: str
    case_status: str
    parish_name: str


class CheshireEastSearchResultV1(FrozenModel):
    """One row from the verified same-document result table."""

    public_reference: str
    application_type: str
    location: str
    proposal: str
    consultation_close: str | None = None
    detail_locator: str


class CheshireEastWeeklyRowV1(FrozenModel):
    """One official weekly-list row retained as source-contract evidence."""

    public_reference: str
    detail_locator: str


class CheshireEastWeeklyBoundaryV1(FrozenModel):
    """Observed weekly rows plus only source-published terminal signals."""

    rows: tuple[CheshireEastWeeklyRowV1, ...]
    reported_total: int | None
    pagination_links: tuple[str, ...]
    terminal_marker: bool


class CheshireEastDocumentMetadataV1(FrozenModel):
    """One document index row without attachment content."""

    document_type: str
    description: str
    published_date: date
    url: HttpUrl


class CheshireEastDetailContractV1(FrozenModel):
    """Reference-verified fields exposed by one official detail response."""

    public_reference: str
    application_status: str
    valid_date: date
    grid_reference: tuple[float, float]
    documents: tuple[CheshireEastDocumentMetadataV1, ...]


class CheshireEastAdapter:
    """Own Cheshire East valid-date form and incomplete-table semantics."""

    manifest = AuthorityManifest(
        id=AuthorityId("cheshire-east"),
        name="Cheshire East Council",
        kind=AuthorityKind.UNITARY,
        sources=(
            SourceDefinition(
                id=SOURCE,
                base_url=HttpUrl(_SEARCH_URL),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CheshireEastCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CheshireEastCheckpointV1]]:
        """Use fixtures or capture the verified valid-date result table."""
        if session.mode == TransportMode.FIXTURE:
            async for batch in self._discover_fixture(session, window, checkpoint):
                yield batch
            return
        raise CheshireEastResultCompletenessUnavailableError

    async def _discover_fixture(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CheshireEastCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CheshireEastCheckpointV1]]:
        page = "1" if checkpoint is None else checkpoint.search_page
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
            for value in re.findall(r'data-cheshire-reference="([^"]+)"', html)
        )
        next_page = _required_fixture(html, r'data-cheshire-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=CheshireEastCheckpointV1(search_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CheshireEastApplicationV1]:
        """Preserve fixture detail and reject the unresolved live interaction."""
        if session.mode != TransportMode.FIXTURE:
            if reference.source_id != SOURCE or reference.locator is None:
                raise CheshireEastRoutingError(reference.reference)
            raise CheshireEastDetailUnavailableError(reference.reference)
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{BASE_URL}/application/{encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        payload = CheshireEastApplicationV1(
            public_reference=reference.reference,
            alternative_reference=_required_fixture(
                html,
                r'data-cheshire-alt="([^"]+)"',
                "alternative reference",
            ),
            development_proposal=unescape(
                _required_fixture(html, r'data-cheshire-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required_fixture(
                html, r'data-cheshire-status="([^"]+)"', "status"
            ),
            parish_name=_required_fixture(
                html, r'data-cheshire-parish="([^"]+)"', "parish"
            ),
        )
        unavailable = UnavailableSection(reason="detail sections not verified live")
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

    def normalise(
        self,
        snapshot: NativeSnapshot[CheshireEastApplicationV1],
    ) -> NormalisedObservation:
        """Map Cheshire East fixture fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.development_proposal,
            status=snapshot.payload.case_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="cheshire-east-v1",
        )


def _parse_search_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form#form")
    if not isinstance(form, Tag):
        _raise_parse("search form")
    if str(form.get("method", "get")).casefold() != "post":
        raise CheshireEastFormMethodUnavailableError
    if form.get("name") != "form":
        _raise_parse("search form name")
    if urljoin(f"{BASE_URL}/", str(form.get("action", ""))) != _SEARCH_POST_URL:
        _raise_parse("search form action")
    for name in ("fa", "submitted", "valid_date_from", "valid_date_to"):
        _unique_named_control(form, name)
    return form


def _named_control(form: Tag, name: str) -> Tag:
    control = form.find(None, {"name": name})
    if not isinstance(control, Tag):
        _raise_parse(name)
    return control


def _unique_named_control(form: Tag, name: str) -> Tag:
    controls = form.find_all(None, {"name": name})
    if len(controls) != 1 or not isinstance(controls[0], Tag):
        _raise_parse(name)
    return controls[0]


def _valid_date_request(form: Tag, window: DiscoveryWindow) -> PortalRequest:
    values = {
        "valid_date_from": window.start.strftime("%d-%m-%Y"),
        "valid_date_to": window.end.strftime("%d-%m-%Y"),
    }
    return PortalRequest(
        url=HttpUrl(_SEARCH_POST_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_successful_form_fields(form, values),
    )


def _successful_form_fields(
    form: Tag, overrides: dict[str, str]
) -> tuple[FormField, ...]:
    fields: list[FormField] = []
    for control in form.select("input[name], select[name], textarea[name]"):
        if control.has_attr("disabled"):
            continue
        name = str(control["name"])
        if name in overrides:
            values: tuple[str, ...] = (overrides[name],)
        elif control.name == "select":
            options: list[Tag] = list(control.select("option[selected]"))
            if not options and not control.has_attr("multiple"):
                options = list(control.select("option")[:1])
            values = tuple(str(option.get("value", "")) for option in options)
        elif control.name == "textarea":
            values = (control.get_text(),)
        else:
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "file", "image", "reset", "submit"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            values = (str(control.get("value", "")),)
        fields.extend(FormField(name=name, value=value) for value in values)
    return tuple(fields)


def _parse_weekly_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    forms = tuple(form for form in soup.select("form") if form.select('[name="week"]'))
    if len(forms) != 1:
        _raise_parse("weekly received form")
    form = forms[0]
    if (
        str(form.get("method", "get")).casefold() != "post"
        or urljoin(f"{BASE_URL}/", str(form.get("action", ""))) != _WEEKLY_RECEIVED_URL
    ):
        _raise_parse("weekly received form")
    _unique_named_control(form, "week")
    _unique_named_control(form, "fa")
    return form


def _weekly_received_request(form: Tag, week: date) -> PortalRequest:
    return PortalRequest(
        url=HttpUrl(_WEEKLY_RECEIVED_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_successful_form_fields(form, {"week": week.strftime("%d-%m-%Y")}),
    )


def _parse_weekly_boundary(body: bytes) -> CheshireEastWeeklyBoundaryV1:
    soup = BeautifulSoup(body, "html.parser")
    expected_headers = (
        "application",
        "location details",
        "proposal",
        "ward",
        "community",
        "consultation end date",
        "publicity end date",
        "details available",
        "jump to application",
    )
    matches = []
    for table in soup.select("table"):
        first_row = table.select_one("tr")
        if first_row is None:
            continue
        headers = tuple(
            _normalise_label(cell.get_text(" ", strip=True))
            for cell in first_row.find_all(("th", "td"), recursive=False)
        )
        if headers == expected_headers:
            matches.append(table)
    if len(matches) != 1:
        _raise_parse("weekly received table")
    rows = []
    for row in matches[0].select("tr")[1:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(expected_headers):
            _raise_parse("weekly received row columns")
        links = cells[-1].select("a[href]")
        if len(links) != 1:
            _raise_parse("weekly received detail link")
        locator = _detail_locator(str(links[0]["href"]))
        rows.append(
            CheshireEastWeeklyRowV1(
                public_reference=cells[0].get_text(" ", strip=True),
                detail_locator=locator,
            )
        )
    pagination_links = tuple(
        str(link["href"])
        for link in soup.select('.pagination a[href], a[rel="next"], a[rel="prev"]')
    )
    total = _reported_total(soup)
    terminal_marker = any(
        _normalise_label(element.get_text(" ", strip=True))
        in {"all applications loaded", "all results loaded"}
        for element in soup.select("button, [role='status']")
    )
    return CheshireEastWeeklyBoundaryV1(
        rows=tuple(rows),
        reported_total=total,
        pagination_links=pagination_links,
        terminal_marker=terminal_marker,
    )


def _detail_locator(href: str) -> str:
    split = urlsplit(urljoin(f"{BASE_URL}/", href))
    values = parse_qs(split.query)
    if (
        split.scheme != "https"
        or split.netloc != "pa.cheshireeast.gov.uk"
        or split.path != "/planning/index.html"
        or values.get("fa") != ["getApplication"]
        or len(values.get("id", [])) != 1
        or not values["id"][0].isdigit()
    ):
        _raise_parse("weekly received detail link")
    return values["id"][0]


def _reported_total(soup: BeautifulSoup) -> int | None:
    nodes = soup.select("[data-result-count]")
    if not nodes:
        return None
    if len(nodes) != 1 or not str(nodes[0].get("data-result-count", "")).isdigit():
        _raise_parse("reported result total")
    return int(str(nodes[0]["data-result-count"]))


def _parse_detail_contract(
    body: bytes,
    *,
    expected_reference: str,
    expected_locator: str,
) -> CheshireEastDetailContractV1:
    soup = BeautifulSoup(body, "html.parser")
    containers = soup.select("#application_details[data-application-id]")
    if (
        len(containers) != 1
        or containers[0].get("data-application-id") != expected_locator
    ):
        _raise_parse("application detail locator")
    fields = _detail_fields(containers[0])
    published_reference = _required_detail_field(fields, "application reference number")
    if published_reference != expected_reference:
        raise CheshireEastReferenceMismatchError(
            expected_reference, published_reference
        )
    grid = _grid_reference(_required_detail_field(fields, "grid reference"))
    documents = _parse_document_metadata(soup, expected_locator)
    return CheshireEastDetailContractV1(
        public_reference=published_reference,
        application_status=_required_detail_field(fields, "application status"),
        valid_date=_portal_date(_required_detail_field(fields, "valid date")),
        grid_reference=grid,
        documents=documents,
    )


def _detail_fields(container: Tag) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in container.select(".row.pad-bottom-5"):
        label = row.select_one("strong")
        value = row.select_one(".col-md-7")
        if label is None or value is None:
            _raise_parse("application detail row")
        key = _normalise_label(label.get_text(" ", strip=True))
        if key in fields:
            _raise_parse("application detail duplicate field")
        fields[key] = unescape(value.get_text(" ", strip=True))
    return fields


def _required_detail_field(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "")
    if not value:
        _raise_parse(name)
    return value


def _portal_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d-%m-%Y").replace(tzinfo=UTC).date()
    except ValueError:
        _raise_parse("portal date")


def _grid_reference(value: str) -> tuple[float, float]:
    pattern = r"\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*"
    match = re.fullmatch(pattern, value)
    if match is None:
        _raise_parse("grid reference")
    return float(match.group(1)), float(match.group(2))


def _parse_document_metadata(
    soup: BeautifulSoup, expected_locator: str
) -> tuple[CheshireEastDocumentMetadataV1, ...]:
    table = soup.select_one("table#application_documents")
    loaded = soup.select_one("#all_documents_loaded_application_documents[disabled]")
    show_more = soup.select_one("#show_more_documents_application_documents")
    if (
        not isinstance(table, Tag)
        or loaded is None
        or show_more is None
        or "display:none"
        not in str(show_more.get("style", "")).replace(" ", "").casefold()
    ):
        _raise_parse("complete document table")
    expected_headers = (
        "document type",
        "description",
        "thumbnail",
        "date document added",
        "download/view",
    )
    rows = table.select("tr")
    if not rows:
        _raise_parse("document table headers")
    headers = tuple(
        _normalise_label(cell.get_text(" ", strip=True))
        for cell in rows[0].find_all(("th", "td"), recursive=False)
    )
    if headers != expected_headers:
        _raise_parse("document table headers")
    documents = []
    for row in rows[1:]:
        cells = row.find_all("td", recursive=False)
        fields = tuple(str(cell.get("data-field-name", "")) for cell in cells)
        if fields != (
            "document_type",
            "description",
            "thumbnail",
            "date_document_added",
            "download",
        ):
            _raise_parse("document row columns")
        links = cells[-1].select("a[href]")
        if len(links) != 1:
            _raise_parse("document metadata link")
        url = urljoin(f"{BASE_URL}/", str(links[0]["href"]))
        _assert_document_url(url, expected_locator)
        documents.append(
            CheshireEastDocumentMetadataV1(
                document_type=cells[0].get_text(" ", strip=True),
                description=cells[1].get_text(" ", strip=True),
                published_date=_portal_date(cells[3].get_text(" ", strip=True)),
                url=HttpUrl(url),
            )
        )
    return tuple(documents)


def _assert_document_url(url: str, expected_locator: str) -> None:
    split = urlsplit(url)
    values = parse_qs(split.query)
    if (
        split.scheme != "https"
        or split.netloc != "pa.cheshireeast.gov.uk"
        or split.path != "/planning/"
        or values.get("fa") != ["downloadDocument"]
        or len(values.get("id", [])) != 1
        or not values["id"][0].isdigit()
        or values.get("public_record_id") != [expected_locator]
    ):
        _raise_parse("document metadata link")


def _parse_result_table(body: bytes) -> tuple[CheshireEastSearchResultV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("table#application_results_table")
    if isinstance(table, Tag):
        rows = table.select("tr")
        if rows:
            headers = tuple(
                _normalise_label(cell.get_text(" ", strip=True))
                for cell in rows[0].find_all(("th", "td"), recursive=False)
            )
            results = _parse_table_rows(tuple(rows[1:]), headers)
            if results:
                return results
    return _raise_parse("valid-date result table")


def _parse_table_rows(
    rows: tuple[Tag, ...], headers: tuple[str, ...]
) -> tuple[CheshireEastSearchResultV1, ...]:
    results = []
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            _raise_parse("result row columns")
        values = {
            header: cell.get_text(" ", strip=True)
            for header, cell in zip(headers, cells, strict=True)
        }
        view = row.select_one("button.view_application[data-id]")
        if not isinstance(view, Tag):
            _raise_parse("View detail locator")
        results.append(
            CheshireEastSearchResultV1(
                public_reference=_required_mapping(values, "reference"),
                application_type=_required_mapping(values, "application type", "type"),
                location=_required_mapping(values, "location", "address"),
                proposal=_required_mapping(values, "proposal"),
                consultation_close=_optional_mapping(
                    values, "consultation close", "consultation close date"
                ),
                detail_locator=str(view.get("data-id", "")),
            )
        )
    return tuple(results)


def _optional_mapping(values: dict[str, str], *needles: str) -> str | None:
    for needle in needles:
        for label, value in values.items():
            if needle in label and value:
                return unescape(value)
    return None


def _required_mapping(values: dict[str, str], *needles: str) -> str:
    value = _optional_mapping(values, *needles)
    if value is None:
        _raise_parse(f"result {'/'.join(needles)}")
    return value


def _normalise_label(value: str) -> str:
    return " ".join(value.strip().rstrip(":").casefold().split())


def _assert_window(
    checkpoint: CheshireEastCheckpointV1, window: DiscoveryWindow
) -> None:
    if checkpoint.window_start is not None and (
        checkpoint.window_start != window.start or checkpoint.window_end != window.end
    ):
        raise CheshireEastCheckpointError


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class CheshireEastParseError(ValueError):
    """A required Cheshire East boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Cheshire East field {field}")


class CheshireEastFormMethodUnavailableError(RuntimeError):
    """The live form no longer uses the supported POST boundary."""


class CheshireEastResultCompletenessUnavailableError(RuntimeError):
    """Count and pagination rules remain unresolved after the observed table."""

    def __init__(self) -> None:
        """Prevent the visible rows from becoming false completeness."""
        super().__init__("Cheshire East result count and pagination are unresolved")


class CheshireEastDetailUnavailableError(RuntimeError):
    """The observed View interaction did not produce a readable detail."""

    def __init__(self, reference: str) -> None:
        """Identify the public reference only."""
        super().__init__(f"Cheshire East detail is unresolved for {reference}")


class CheshireEastReferenceMismatchError(ValueError):
    """The direct detail response belongs to another public reference."""

    def __init__(self, expected: str, published: str) -> None:
        """Name both non-sensitive public references."""
        super().__init__(
            f"Cheshire East detail reference {published} did not match {expected}"
        )


class CheshireEastRoutingError(ValueError):
    """A reference lacks the verified View locator."""

    def __init__(self, reference: str) -> None:
        """Identify the public reference only."""
        super().__init__(f"Cheshire East cannot route reference {reference}")


class CheshireEastCheckpointError(ValueError):
    """A saved table cursor belongs to another date window."""


def _raise_parse(field: str) -> NoReturn:
    raise CheshireEastParseError(field)
