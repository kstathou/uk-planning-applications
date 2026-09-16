# Copyright (c) 2026 Kostas Stathoulopoulos

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from math import isfinite
from typing import TYPE_CHECKING, NoReturn, cast
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
_MAX_BNG_EASTING = 700_000
_MAX_BNG_NORTHING = 1_300_000
_SEARCH_FILTER_NAMES = frozenset(
    {
        "application_reference_number",
        "application_type_id",
        "proposal",
        "decision_type_id",
        "Applicant[applicant_name]",
        "Applicant[company_name]",
        "Agent[agent_name]",
        "Agent[company_name]",
        "ps_development_code_id",
        "SiteAddress[magic]",
        "SiteAddress[postcode]",
        "SiteAddress[Street][street_description]",
        "site_address_description",
        "site_address_x",
        "site_address_y",
        "ward_id",
        "community_id",
        "received_date_from",
        "received_date_to",
        "committee_proposed_date_from",
        "committee_proposed_date_to",
        "decision_issued_date_from",
        "decision_issued_date_to",
    }
)


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


class CheshireEastSearchBoundaryV1(FrozenModel):
    """Visible search rows or one exact source-published zero marker."""

    results: tuple[CheshireEastSearchResultV1, ...]
    explicit_zero: bool
    reported_total: int | None
    pagination_links: tuple[str, ...]
    terminal_marker: bool


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
        """Use fixture discovery or reject live collection before source I/O."""
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
        """Preserve fixture detail or reject live collection for this authority."""
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


def parse_search_form(body: bytes) -> Tag:
    """Validate the exact official general-search form boundary."""
    soup = BeautifulSoup(body, "html.parser")
    forms = soup.select("form#form")
    if len(forms) != 1:
        _raise_parse("search form")
    form = forms[0]
    _reject_external_form_controls(soup, form)
    if str(form.get("method", "get")).casefold() != "post":
        raise CheshireEastFormMethodUnavailableError
    if _effective_form_enctype(form) != "application/x-www-form-urlencoded":
        _raise_parse("search form encoding")
    if form.get("name") != "form":
        _raise_parse("search form name")
    if (
        _source_url(
            f"{BASE_URL}/",
            str(form.get("action", "")),
            "search form action",
        )
        != _SEARCH_POST_URL
    ):
        _raise_parse("search form action")
    controls = {
        name: _enabled_named_control(form, name)
        for name in ("fa", "submitted", "valid_date_from", "valid_date_to")
    }
    _require_input_type(controls["fa"], {"hidden"})
    _require_input_type(controls["submitted"], {"hidden"})
    _require_input_type(controls["valid_date_from"], {"", "text"})
    _require_input_type(controls["valid_date_to"], {"", "text"})
    if controls["fa"].get("value") != "search":
        _raise_parse("search form discriminator")
    successful_names = _successful_field_names(form)
    if len(successful_names) != len(set(successful_names)):
        _raise_parse("duplicate successful search control")
    _exact_search_form_fields(
        form,
        {"valid_date_from": "", "valid_date_to": ""},
    )
    return form


def _unique_named_control(form: Tag, name: str) -> Tag:
    controls = form.find_all(None, {"name": name})
    if len(controls) != 1 or not isinstance(controls[0], Tag):
        _raise_parse(name)
    return controls[0]


def _enabled_named_control(form: Tag, name: str) -> Tag:
    control = _unique_named_control(form, name)
    if _is_effectively_disabled(control):
        _raise_parse(name)
    return control


def _require_input_type(control: Tag, allowed_types: set[str]) -> None:
    if control.name != "input" or str(control.get("type", "")).casefold() not in (
        allowed_types
    ):
        _raise_parse(f"{control.get('name', '')} input type")


def _is_effectively_disabled(control: Tag) -> bool:
    if control.has_attr("disabled"):
        return True
    for fieldset in control.find_parents("fieldset"):
        if not fieldset.has_attr("disabled"):
            continue
        first_legend = fieldset.find("legend", recursive=False)
        if isinstance(first_legend, Tag) and first_legend in control.parents:
            continue
        return True
    return False


def _reject_external_form_controls(soup: BeautifulSoup, form: Tag) -> None:
    form_id = str(form.get("id", ""))
    for control in form.select("button, input, select, textarea"):
        explicit_owner = str(control.get("form", ""))
        if control.has_attr("form") and (
            not explicit_owner or explicit_owner != form_id
        ):
            _raise_parse("reassigned form control")
    if not form_id:
        return
    for control in soup.select(f'[form="{form_id}"]'):
        if control.name in {"button", "input", "select", "textarea"} and form not in (
            control,
            *control.parents,
        ):
            _raise_parse("external associated form control")


def _effective_form_enctype(form: Tag) -> str:
    return (
        str(form.get("enctype", "application/x-www-form-urlencoded")).strip().casefold()
    )


def valid_date_request(form: Tag, window: DiscoveryWindow) -> PortalRequest:
    """Replay successful controls with both requested valid-date bounds."""
    values = {
        "valid_date_from": window.start.strftime("%d-%m-%Y"),
        "valid_date_to": window.end.strftime("%d-%m-%Y"),
    }
    return PortalRequest(
        url=HttpUrl(_SEARCH_POST_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_exact_search_form_fields(form, values),
    )


def search_form_request() -> PortalRequest:
    """Build the official general-search form request."""
    return PortalRequest(url=HttpUrl(_SEARCH_URL), intent=RequestIntent.SEARCH)


def _successful_form_fields(
    form: Tag, overrides: dict[str, str]
) -> tuple[FormField, ...]:
    fields: list[FormField] = []
    for control in form.select("input[name], select[name], textarea[name]"):
        if _is_effectively_disabled(control):
            continue
        name = str(control["name"])
        if name in overrides:
            values: tuple[str, ...] = (overrides[name],)
        elif control.name == "select":
            selected = control.select("option[selected]")
            options = [option for option in selected if not _is_disabled_option(option)]
            if not selected and not control.has_attr("multiple"):
                options = [
                    option
                    for option in control.select("option")
                    if not _is_disabled_option(option)
                ][:1]
            values = tuple(_option_value(option) for option in options)
        elif control.name == "textarea":
            values = (control.get_text(),)
        else:
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "file", "image", "reset", "submit"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            values = (
                (
                    "on"
                    if input_type in {"checkbox", "radio"}
                    and not control.has_attr("value")
                    else str(control.get("value", ""))
                ),
            )
        fields.extend(FormField(name=name, value=value) for value in values)
    return tuple(fields)


def _exact_search_form_fields(
    form: Tag,
    date_overrides: dict[str, str],
) -> tuple[FormField, ...]:
    fields: list[FormField] = []
    for control in form.select("input[name], select[name], textarea[name]"):
        if _is_effectively_disabled(control):
            continue
        name = str(control["name"])
        values = _exact_search_control_values(control, name, date_overrides)
        fields.extend(FormField(name=name, value=value) for value in values)
    return tuple(fields)


def _exact_search_control_values(
    control: Tag,
    name: str,
    date_overrides: dict[str, str],
) -> tuple[str, ...]:
    if name in date_overrides:
        return (date_overrides[name],)
    if name in {"fa", "submitted"}:
        return (str(control.get("value", "")),)
    if name in _SEARCH_FILTER_NAMES:
        return _neutral_search_filter_values(control, name)
    input_type = str(control.get("type", "text")).casefold()
    if control.name == "input" and input_type in {
        "button",
        "file",
        "image",
        "reset",
        "submit",
    }:
        return ()
    return _raise_parse(f"unknown {name} search control")


def _neutral_search_filter_values(control: Tag, name: str) -> tuple[str, ...]:
    if control.name == "select":
        if control.has_attr("multiple"):
            return ()
        blank_options = tuple(
            option
            for option in control.select("option")
            if not _is_disabled_option(option) and _option_value(option) == ""
        )
        if len(blank_options) != 1:
            _raise_parse(f"neutral {name} search option")
        return ("",)
    if control.name == "textarea":
        return ("",)
    input_type = str(control.get("type", "text")).casefold()
    return () if input_type in {"checkbox", "radio"} else ("",)


def _is_disabled_option(option: Tag) -> bool:
    parent = option.parent
    return option.has_attr("disabled") or (
        isinstance(parent, Tag)
        and parent.name == "optgroup"
        and parent.has_attr("disabled")
    )


def _option_value(option: Tag) -> str:
    if option.has_attr("value"):
        return str(option["value"])
    return " ".join(option.get_text(" ", strip=True).split())


def _successful_field_names(form: Tag) -> tuple[str, ...]:
    return tuple(field.name for field in _successful_form_fields(form, {}))


def parse_weekly_form(body: bytes) -> Tag:
    """Validate the official weekly-received form boundary."""
    soup = BeautifulSoup(body, "html.parser")
    forms = tuple(form for form in soup.select("form") if form.select('[name="week"]'))
    if len(forms) != 1:
        _raise_parse("weekly received form")
    form = forms[0]
    _reject_external_form_controls(soup, form)
    if (
        str(form.get("method", "get")).casefold() != "post"
        or _effective_form_enctype(form) != "application/x-www-form-urlencoded"
        or _source_url(
            f"{BASE_URL}/",
            str(form.get("action", "")),
            "weekly received form",
        )
        != _WEEKLY_RECEIVED_URL
    ):
        _raise_parse("weekly received form")
    week = _enabled_named_control(form, "week")
    _require_input_type(week, {"text"})
    discriminator = _enabled_named_control(form, "fa")
    _require_input_type(discriminator, {"hidden"})
    if discriminator.get("value") != "":
        _raise_parse("weekly received discriminator")
    if _successful_field_names(form) != ("week", "fa"):
        _raise_parse("weekly received successful controls")
    return form


def weekly_received_request(form: Tag, week: date) -> PortalRequest:
    """Replay the weekly-received form for one exact Monday."""
    return PortalRequest(
        url=HttpUrl(_WEEKLY_RECEIVED_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=_successful_form_fields(form, {"week": week.strftime("%d-%m-%Y")}),
    )


def weekly_received_form_request() -> PortalRequest:
    """Build the official weekly-received form request."""
    return PortalRequest(
        url=HttpUrl(_WEEKLY_RECEIVED_URL),
        intent=RequestIntent.SEARCH,
    )


def detail_request(locator: str) -> PortalRequest:
    """Build a direct official detail request from a numeric locator."""
    if not locator.isdigit():
        _raise_parse("application detail locator")
    return PortalRequest(
        url=HttpUrl(_DETAIL_URL.format(locator=locator)),
        intent=RequestIntent.DETAIL,
    )


def parse_search_boundary(body: bytes) -> CheshireEastSearchBoundaryV1:
    """Parse visible search rows or one exact no-results marker."""
    soup = BeautifulSoup(body, "html.parser")
    containers = soup.select("div.application-list")
    if len(containers) != 1:
        _raise_parse("search result boundary")
    container = containers[0]
    tables = soup.select("table#application_results_table")
    scoped_tables = container.select("table#application_results_table")
    if tables:
        if (
            len(tables) != 1
            or len(scoped_tables) != 1
            or tables[0] is not scoped_tables[0]
            or set(map(str, container.get_attribute_list("class")))
            != {"centered", "application-list"}
            or container.select("div.push-30-t > strong.text-danger")
            or _has_hidden_ancestor(tables[0])
        ):
            _raise_parse("search result boundary")
        results = _parse_result_table(tables[0])
        if _has_unverified_total_signal(container):
            _raise_parse("unverified result total")
        return CheshireEastSearchBoundaryV1(
            results=results,
            explicit_zero=False,
            reported_total=None,
            pagination_links=_pagination_links(container),
            terminal_marker=False,
        )
    zero_markers = container.select("div.push-30-t > strong.text-danger")
    if (
        len(scoped_tables) != 0
        or len(zero_markers) != 1
        or set(map(str, container.get_attribute_list("class")))
        != {"col-sm-12", "col-md-12", "animation-fadeIn", "application-list"}
    ):
        _raise_parse("search result boundary")
    marker = zero_markers[0]
    marker_parent = cast("Tag", marker.parent)
    if _pagination_links(container) or _has_unverified_total_signal(container):
        _raise_parse("zero result boundary")
    if (
        marker_parent.parent is not container
        or set(map(str, marker_parent.get_attribute_list("class"))) != {"push-30-t"}
        or set(map(str, marker.get_attribute_list("class"))) != {"text-danger"}
        or _normalise_label(marker.get_text(" ", strip=True)) != "no results found."
        or container.get_text(" ", strip=True) != marker.get_text(" ", strip=True)
        or _direct_tags(container) != (marker_parent,)
        or _direct_tags(marker_parent) != (marker,)
        or _direct_tags(marker)
        or not _all_rendered([marker])
        or container.select("script, style, template, title, noscript")
    ):
        _raise_parse("search result boundary")
    return CheshireEastSearchBoundaryV1(
        results=(),
        explicit_zero=True,
        reported_total=0,
        pagination_links=(),
        terminal_marker=True,
    )


def _has_hidden_ancestor(element: Tag) -> bool:
    return any(
        _is_hidden_markup(candidate) for candidate in (element, *element.parents)
    )


def _is_hidden_markup(element: Tag) -> bool:
    style = _style_declarations(element)
    classes = {
        str(value).strip().casefold() for value in element.get_attribute_list("class")
    }
    return (
        element.name
        in {
            "head",
            "input",
            "noscript",
            "option",
            "script",
            "select",
            "style",
            "template",
            "title",
        }
        or (element.name == "dialog" and not element.has_attr("open"))
        or (element.name == "details" and not element.has_attr("open"))
        or element.has_attr("hidden")
        or bool(classes & {"d-none", "hide", "hidden", "invisible"})
        or ("collapse" in classes and not classes & {"in", "show"})
        or str(element.get("aria-hidden", "")).strip().casefold() == "true"
        or _css_value(style.get("display")) == "none"
        or _css_value(style.get("visibility")) in {"hidden", "collapse"}
    )


def _direct_tags(element: Tag) -> tuple[Tag, ...]:
    return tuple(child for child in element.children if isinstance(child, Tag))


def _has_hidden_descendant(element: Tag) -> bool:
    return any(
        _is_hidden_markup(descendant)
        for descendant in element.descendants
        if isinstance(descendant, Tag)
    )


def _style_declarations(element: Tag) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for declaration in str(element.get("style", "")).split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            normalised_name = name.strip().casefold()
            if normalised_name in declarations:
                _raise_parse("duplicate inline style declaration")
            declarations[normalised_name] = value.strip().casefold()
    return declarations


def _css_value(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s*!important\s*$", "", value).strip()


def parse_weekly_boundary(body: bytes) -> CheshireEastWeeklyBoundaryV1:
    """Record weekly rows without inferring a missing terminal signal."""
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
        header_cells = first_row.find_all(("th", "td"), recursive=False)
        headers = tuple(
            _normalise_label(cell.get_text(" ", strip=True)) for cell in header_cells
        )
        if headers == expected_headers:
            matches.append(table)
    if len(matches) != 1:
        _raise_parse("weekly received table")
    matched_header = matches[0].select_one("tr")
    if (
        _has_hidden_ancestor(matches[0])
        or matched_header is None
        or not _all_rendered(matched_header.find_all(("th", "td"), recursive=False))
    ):
        _raise_parse("weekly received table")
    rows = _parse_weekly_rows(matches[0], len(expected_headers))
    table = matches[0]
    parent = table.parent
    boundary = (
        parent
        if isinstance(parent, Tag) and parent.name not in {"[document]", "body", "html"}
        else table
    )
    pagination_links = _pagination_links(boundary)
    return CheshireEastWeeklyBoundaryV1(
        rows=rows,
        reported_total=None,
        pagination_links=pagination_links,
        terminal_marker=False,
    )


def _parse_weekly_rows(
    table: Tag,
    expected_column_count: int,
) -> tuple[CheshireEastWeeklyRowV1, ...]:
    rows = []
    references: set[str] = set()
    locators: set[str] = set()
    for row in table.select("tr")[1:]:
        if _has_hidden_ancestor(row):
            _raise_parse("weekly received row")
        cells = row.find_all("td", recursive=False)
        if len(cells) != expected_column_count or not _all_rendered(cells):
            _raise_parse("weekly received row columns")
        links = cells[-1].select("a[href]")
        if len(links) != 1 or _has_hidden_ancestor(links[0]):
            _raise_parse("weekly received detail link")
        locator = _detail_locator(str(links[0]["href"]))
        reference = cells[0].get_text(" ", strip=True)
        if not reference or reference in references or locator in locators:
            _raise_parse("weekly received row identity")
        references.add(reference)
        locators.add(locator)
        rows.append(
            CheshireEastWeeklyRowV1(
                public_reference=reference,
                detail_locator=locator,
            )
        )
    return tuple(rows)


def _detail_locator(href: str) -> str:
    split = urlsplit(_source_url(f"{BASE_URL}/", href, "weekly received detail link"))
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


def _has_unverified_total_signal(boundary: Tag | BeautifulSoup) -> bool:
    return boundary.has_attr("data-result-count") or bool(
        boundary.select("[data-result-count]")
    )


def _pagination_links(boundary: Tag | BeautifulSoup) -> tuple[str, ...]:
    links = boundary.select('.pagination a[href], a[rel="next"], a[rel="prev"]')
    if any(_has_hidden_ancestor(link) for link in links):
        _raise_parse("pagination link")
    return tuple(str(link["href"]) for link in links)


def parse_detail_contract(
    body: bytes,
    *,
    expected_reference: str,
    expected_locator: str,
) -> CheshireEastDetailContractV1:
    """Validate one direct detail and its complete metadata-only documents."""
    soup = BeautifulSoup(body, "html.parser")
    containers = soup.select("#application_details[data-application-id]")
    if (
        len(containers) != 1
        or containers[0].get("data-application-id") != expected_locator
        or _has_hidden_ancestor(containers[0])
    ):
        _raise_parse("application detail locator")
    fields = _detail_fields(containers[0])
    published_reference = _required_detail_field(fields, "application reference number")
    if published_reference != expected_reference:
        message = (
            f"Cheshire East detail reference {published_reference} did not match "
            f"{expected_reference}"
        )
        raise CheshireEastReferenceMismatchError(message)
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
        if _has_hidden_ancestor(row):
            _raise_parse("application detail row")
        label = row.select_one("strong")
        value = row.select_one(".col-md-7")
        if label is None or value is None or not _all_rendered([label, value]):
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
    easting, northing = float(match.group(1)), float(match.group(2))
    if (
        not isfinite(easting)
        or not isfinite(northing)
        or not 0 <= easting <= _MAX_BNG_EASTING
        or not 0 <= northing <= _MAX_BNG_NORTHING
    ):
        _raise_parse("grid reference")
    return easting, northing


def _parse_document_metadata(
    soup: BeautifulSoup, expected_locator: str
) -> tuple[CheshireEastDocumentMetadataV1, ...]:
    sections = soup.select("div#documents")
    if len(sections) != 1:
        _raise_parse("complete document table")
    section = sections[0]
    tables = soup.select("table#application_documents")
    section_tables = section.select("table#application_documents")
    loaded_controls = soup.select("#all_documents_loaded_application_documents")
    section_loaded_controls = section.select(
        "#all_documents_loaded_application_documents"
    )
    show_more_controls = soup.select("#show_more_documents_application_documents")
    section_show_more_controls = section.select(
        "#show_more_documents_application_documents"
    )
    if (
        len(tables) != 1
        or len(section_tables) != 1
        or tables[0] is not section_tables[0]
        or len(loaded_controls) != 1
        or len(section_loaded_controls) != 1
        or loaded_controls[0] is not section_loaded_controls[0]
        or loaded_controls[0].name != "button"
        or not loaded_controls[0].has_attr("disabled")
        or len(show_more_controls) != 1
        or len(section_show_more_controls) != 1
        or show_more_controls[0] is not section_show_more_controls[0]
        or show_more_controls[0].name != "button"
        or _normalise_label(loaded_controls[0].get_text(" ", strip=True))
        != "all documents loaded"
        or _normalise_label(show_more_controls[0].get_text(" ", strip=True))
        != "show more"
        or _has_hidden_ancestor(section)
        or _has_hidden_ancestor(tables[0])
        or _has_hidden_ancestor(loaded_controls[0])
        or _has_hidden_descendant(loaded_controls[0])
        or any(_is_hidden_markup(parent) for parent in show_more_controls[0].parents)
        or _has_hidden_descendant(show_more_controls[0])
        or _style_declarations(show_more_controls[0]).get("display") != "none"
    ):
        _raise_parse("complete document table")
    table = tables[0]
    expected_headers = (
        "document type",
        "description",
        "thumbnail",
        "date document added",
        "download/view",
    )
    rows = table.select("tr")
    _validate_document_headers(rows, expected_headers)
    documents = []
    for row in rows[1:]:
        if _has_hidden_ancestor(row):
            _raise_parse("document row")
        cells = row.find_all("td", recursive=False)
        if not _all_rendered(cells):
            _raise_parse("document row")
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
        if len(links) != 1 or _has_hidden_ancestor(links[0]):
            _raise_parse("document metadata link")
        url = _source_url(
            f"{BASE_URL}/",
            str(links[0]["href"]),
            "document metadata link",
        )
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


def _validate_document_headers(
    rows: list[Tag],
    expected_headers: tuple[str, ...],
) -> None:
    if not rows:
        _raise_parse("document table headers")
    header_cells = rows[0].find_all(("th", "td"), recursive=False)
    headers = tuple(
        _normalise_label(cell.get_text(" ", strip=True)) for cell in header_cells
    )
    if not _all_rendered(header_cells) or headers != expected_headers:
        _raise_parse("document table headers")


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
        or set(values) != {"fa", "id", "public_record_id"}
    ):
        _raise_parse("document metadata link")


def _source_url(base: str, value: str, field: str) -> str:
    try:
        return urljoin(base, value)
    except ValueError:
        _raise_parse(field)


def _parse_result_table(
    table: Tag | bytes,
) -> tuple[CheshireEastSearchResultV1, ...]:
    if isinstance(table, bytes):
        soup = BeautifulSoup(table, "html.parser")
        tables = soup.select("table#application_results_table")
        if len(tables) != 1:
            return _raise_parse("valid-date result table")
        table = tables[0]
    rows = table.select("tr")
    if not rows or _has_hidden_ancestor(rows[0]):
        return _raise_parse("valid-date result table")
    header_cells = rows[0].find_all(("th", "td"), recursive=False)
    if not _all_rendered(header_cells):
        return _raise_parse("valid-date result table headers")
    headers = tuple(
        _normalise_label(cell.get_text(" ", strip=True)) for cell in header_cells
    )
    expected_headers = (
        "reference",
        "application type",
        "location",
        "proposal",
        "view",
    )
    if headers != expected_headers:
        return _raise_parse("valid-date result table headers")
    results = _parse_table_rows(tuple(rows[1:]), headers)
    if not results:
        return _raise_parse("valid-date result table")
    return results


def _parse_table_rows(
    rows: tuple[Tag, ...], headers: tuple[str, ...]
) -> tuple[CheshireEastSearchResultV1, ...]:
    results = []
    references: set[str] = set()
    locators: set[str] = set()
    for row in rows:
        if _has_hidden_ancestor(row):
            _raise_parse("result row")
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers) or not _all_rendered(cells):
            _raise_parse("result row columns")
        values = {
            header: cell.get_text(" ", strip=True)
            for header, cell in zip(headers, cells, strict=True)
        }
        row_views = row.select("button.view_application")
        views = cells[-1].select("button.view_application")
        if (
            len(row_views) != 1
            or row_views != views
            or len(views) != 1
            or _has_hidden_ancestor(views[0])
            or not str(views[0].get("data-id", "")).isdigit()
        ):
            _raise_parse("View detail locator")
        view = views[0]
        reference = _required_mapping(values, "reference")
        locator = str(view["data-id"])
        if reference in references or locator in locators:
            _raise_parse("result row identity")
        references.add(reference)
        locators.add(locator)
        results.append(
            CheshireEastSearchResultV1(
                public_reference=reference,
                application_type=_required_mapping(values, "application type", "type"),
                location=_required_mapping(values, "location", "address"),
                proposal=_required_mapping(values, "proposal"),
                consultation_close=_optional_mapping(
                    values, "consultation close", "consultation close date"
                ),
                detail_locator=locator,
            )
        )
    return tuple(results)


def _all_rendered(elements: list[Tag]) -> bool:
    return all(
        not _has_hidden_ancestor(element) and not _has_hidden_descendant(element)
        for element in elements
    )


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


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class CheshireEastParseError(ValueError):
    """A required Cheshire East boundary value was absent."""

    def __init__(self, field: str) -> None:
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Cheshire East field {field}")


class CheshireEastFormMethodUnavailableError(RuntimeError):
    """The live form no longer uses the supported POST boundary."""


class CheshireEastResultCompletenessUnavailableError(RuntimeError):
    """Live discovery is blocked because result completeness is unproved."""

    def __init__(self) -> None:
        super().__init__("Cheshire East live discovery completeness is unproved")


class CheshireEastDetailUnavailableError(RuntimeError):
    """Live detail collection is disabled while the authority is blocked."""

    def __init__(self, reference: str) -> None:
        super().__init__(f"Cheshire East live detail is blocked for {reference}")


class CheshireEastReferenceMismatchError(ValueError):
    """The direct detail response belongs to another public reference."""


class CheshireEastRoutingError(ValueError):
    """A reference lacks the verified View locator."""

    def __init__(self, reference: str) -> None:
        super().__init__(f"Cheshire East cannot route reference {reference}")


def _raise_parse(field: str) -> NoReturn:
    raise CheshireEastParseError(field)
