# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden-owned GeneralSearch query and checkpoint semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date  # noqa: TC003 - Pydantic resolves this annotation at runtime.
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, Self
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import ConfigDict, Field, HttpUrl, RootModel, model_validator

from yimby.domain import DiscoveryWindow, FrozenModel
from yimby.transport import FormField, PortalRequest, RequestIntent, RequestMethod

GENERAL_SEARCH_URL = (
    "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx"
)
_GENERAL_SEARCH_PATHS = {
    "/NECSWS/PlanningExplorer/GeneralSearch.aspx",
    "/Northgate/PlanningExplorer17/GeneralSearch.aspx",
}
_RESULT_PAGE_SIZE = 10
_EMPTY_RESULTS = "No Records Found. Please resubmit search with different criteria."
_OVERRIDDEN_CONTROLS = {
    "txtApplicationNumber",
    "txtApplicantName",
    "txtAgentName",
    "txtSiteAddress",
    "cboStreetReferenceNumber",
    "txtProposal",
    "cboWardCode",
    "cboApplicationTypeCode",
    "cboDevelopmentTypeCode",
    "cboStatusCode",
    "cboSelectDateValue",
    "rbGroup",
    "dateStart",
    "dateEnd",
    "edrDateSelection",
}


class _CamdenModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CamdenDateField(StrEnum):
    """Official Camden date fields used by the bounded bootstrap."""

    RECEIVED = "DATE_RECEIVED"
    VALID = "DATE_VALID"
    DECISION = "DATE_DECISION"


class CamdenOpenStatus(StrEnum):
    """Official Camden statuses that remain source-owned and active."""

    REGISTERED = "4"
    APPEAL_LODGED = "14"

    @property
    def label(self) -> str:
        """Return the official visible label for the source code."""
        return {
            CamdenOpenStatus.REGISTERED: "REGISTERED",
            CamdenOpenStatus.APPEAL_LODGED: "APPEAL LODGED",
        }[self]


class CamdenDiscoveryScopeV1(_CamdenModel):
    """Semantic live scope bound into every durable checkpoint."""

    start: date
    end: date
    include_open: bool

    @model_validator(mode="after")
    def _ordered_dates(self) -> Self:
        if self.end < self.start:
            message = "Camden discovery end date precedes start date"
            raise ValueError(message)
        return self

    @classmethod
    def from_window(cls, window: DiscoveryWindow) -> Self:
        """Copy the common window into Camden-owned durable state."""
        return cls(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )


class CamdenDateQueryV1(_CamdenModel):
    """One official inclusive Camden date-range query."""

    kind: Literal["date"] = "date"
    field: CamdenDateField
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered_dates(self) -> Self:
        if self.end < self.start:
            message = "Camden query end date precedes start date"
            raise ValueError(message)
        return self

    @property
    def key(self) -> str:
        """Return a stable semantic query identity."""
        return f"date:{self.field}:{self.start.isoformat()}:{self.end.isoformat()}"


class CamdenStatusQueryV1(_CamdenModel):
    """One official Camden active-status query."""

    kind: Literal["status"] = "status"
    status: CamdenOpenStatus

    @property
    def key(self) -> str:
        """Return a stable semantic query identity."""
        return f"status:{self.status}:{self.status.label}"


CamdenDiscoveryQueryV1 = Annotated[
    CamdenDateQueryV1 | CamdenStatusQueryV1,
    Field(discriminator="kind"),
]


def camden_query_inventory(
    scope: CamdenDiscoveryScopeV1,
) -> tuple[CamdenDiscoveryQueryV1, ...]:
    """Return the exact ordered query inventory for a live scope."""
    queries: list[CamdenDiscoveryQueryV1] = [
        CamdenDateQueryV1(
            field=field,
            start=scope.start,
            end=scope.end,
        )
        for field in (
            CamdenDateField.RECEIVED,
            CamdenDateField.VALID,
            CamdenDateField.DECISION,
        )
    ]
    if scope.include_open:
        queries.extend(
            CamdenStatusQueryV1(status=status)
            for status in (
                CamdenOpenStatus.REGISTERED,
                CamdenOpenStatus.APPEAL_LODGED,
            )
        )
    return tuple(queries)


class CamdenSeenReferenceV1(_CamdenModel):
    """One public reference paired with its stable Northgate key."""

    reference: str = Field(min_length=1)
    locator: str = Field(min_length=1, pattern=r"^\d+$")


class CamdenCompletedQueryV1(_CamdenModel):
    """Reconciled membership and source total for one finished query."""

    query: CamdenDiscoveryQueryV1
    reported_count: int = Field(ge=0)
    ordered_references: tuple[CamdenSeenReferenceV1, ...]

    @model_validator(mode="after")
    def _reconciled_membership(self) -> Self:
        _require_unique_references(
            self.ordered_references,
            context="completed query",
        )
        if len(self.ordered_references) != self.reported_count:
            message = "Camden completed query membership must equal its reported count"
            raise ValueError(message)
        return self


class CamdenBetweenQueriesV1(_CamdenModel):
    """Progress after one reconciled query and before the next POST."""

    kind: Literal["between-queries"] = "between-queries"
    next_query_index: int = Field(ge=0)


class CamdenPagingQueryV1(_CamdenModel):
    """Committed active-query prefix that must be replayed on resume."""

    kind: Literal["paging-query"] = "paging-query"
    query_index: int = Field(ge=0)
    reported_count: int = Field(gt=0)
    next_offset: int = Field(ge=10, multiple_of=10)
    ordered_prefix: tuple[CamdenSeenReferenceV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reconciled_prefix(self) -> Self:
        if len(self.ordered_prefix) != self.next_offset:
            message = "Camden next offset must equal ordered prefix length"
            raise ValueError(message)
        _require_unique_references(
            self.ordered_prefix,
            context="ordered prefix",
        )
        if self.next_offset >= self.reported_count:
            message = "Camden active query offset must precede reported total"
            raise ValueError(message)
        return self


class CamdenTerminalV1(_CamdenModel):
    """Terminal progress with no active query or wire state."""

    kind: Literal["terminal"] = "terminal"


CamdenLiveProgressV1 = Annotated[
    CamdenBetweenQueriesV1 | CamdenPagingQueryV1 | CamdenTerminalV1,
    Field(discriminator="kind"),
]


class CamdenFixtureCheckpointV1(_CamdenModel):
    """Legacy deterministic fixture cursor."""

    kind: Literal["fixture"] = "fixture"
    view_state_page: str = Field(min_length=1)


class CamdenLiveCheckpointV1(_CamdenModel):
    """Complete semantic progress for the Camden live query inventory."""

    kind: Literal["live"] = "live"
    schema_version: Literal[1] = 1
    scope: CamdenDiscoveryScopeV1
    query_inventory: tuple[CamdenDiscoveryQueryV1, ...] = Field(min_length=1)
    completed_queries: tuple[CamdenCompletedQueryV1, ...] = ()
    seen_references: tuple[CamdenSeenReferenceV1, ...] = ()
    progress: CamdenLiveProgressV1

    @model_validator(mode="after")
    def _coherent_progress(self) -> Self:
        expected_inventory = camden_query_inventory(self.scope)
        if self.query_inventory != expected_inventory:
            message = "Camden stored query inventory does not match its scope"
            raise ValueError(message)
        completed = self.completed_queries
        if len(completed) > len(expected_inventory):
            message = "Camden completed query count exceeds inventory"
            raise ValueError(message)
        completed_inventory = tuple(item.query for item in completed)
        if completed_inventory != expected_inventory[: len(completed)]:
            message = "Camden completed queries must be an exact inventory prefix"
            raise ValueError(message)
        active = _active_prefix(self.progress, len(completed), len(expected_inventory))
        union = _reference_union(
            tuple(
                reference
                for result in completed
                for reference in result.ordered_references
            )
            + active
        )
        if self.seen_references != union:
            message = "Camden seen references must equal the deterministic query union"
            raise ValueError(message)
        return self


CamdenCheckpointBranchV1 = Annotated[
    CamdenFixtureCheckpointV1 | CamdenLiveCheckpointV1,
    Field(discriminator="kind"),
]


class CamdenCheckpointV1(RootModel[CamdenCheckpointBranchV1]):
    """Versioned fixture or live Camden checkpoint boundary."""

    root: CamdenCheckpointBranchV1

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fixture(cls, value: object) -> object:
        """Accept only the exact pre-live fixture checkpoint shape."""
        if isinstance(value, dict) and set(value) == {"view_state_page"}:
            return {"kind": "fixture", "view_state_page": value["view_state_page"]}
        return value


def initial_live_checkpoint(window: DiscoveryWindow) -> CamdenCheckpointV1:
    """Create empty live progress bound to the requested scope."""
    scope = CamdenDiscoveryScopeV1.from_window(window)
    return CamdenCheckpointV1(
        root=CamdenLiveCheckpointV1(
            scope=scope,
            query_inventory=camden_query_inventory(scope),
            progress=CamdenBetweenQueriesV1(next_query_index=0),
        )
    )


def _require_unique_references(
    references: tuple[CamdenSeenReferenceV1, ...],
    *,
    context: str,
) -> None:
    by_reference: dict[str, str] = {}
    for item in references:
        if item.reference in by_reference:
            message = f"Camden {context} must contain unique references"
            raise ValueError(message)
        by_reference[item.reference] = item.locator


def _reference_union(
    references: tuple[CamdenSeenReferenceV1, ...],
) -> tuple[CamdenSeenReferenceV1, ...]:
    ordered: list[CamdenSeenReferenceV1] = []
    by_reference: dict[str, str] = {}
    for item in references:
        locator = by_reference.get(item.reference)
        if locator is None:
            by_reference[item.reference] = item.locator
            ordered.append(item)
        elif locator != item.locator:
            message = "Camden public reference changed Northgate locator"
            raise ValueError(message)
    return tuple(ordered)


def _active_prefix(
    progress: CamdenLiveProgressV1,
    completed_count: int,
    inventory_count: int,
) -> tuple[CamdenSeenReferenceV1, ...]:
    if isinstance(progress, CamdenTerminalV1):
        if completed_count != inventory_count:
            message = "Camden terminal checkpoint requires every query result"
            raise ValueError(message)
        return ()
    if isinstance(progress, CamdenBetweenQueriesV1):
        if progress.next_query_index != completed_count:
            message = "Camden next query index must follow completed queries"
            raise ValueError(message)
        if progress.next_query_index >= inventory_count:
            message = "Camden final query must transition to terminal"
            raise ValueError(message)
        return ()
    if progress.query_index != completed_count:
        message = "Camden active query must follow completed queries"
        raise ValueError(message)
    if progress.query_index >= inventory_count:
        message = "Camden active query is outside the inventory"
        raise ValueError(message)
    return progress.ordered_prefix


@dataclass(frozen=True, slots=True)
class _CamdenSearchForm:
    action: HttpUrl
    controls: tuple[FormField, ...]


@dataclass(frozen=True, slots=True)
class _CamdenResultPage:
    reported_count: int
    references: tuple[CamdenSeenReferenceV1, ...]
    next_url: HttpUrl | None


def _parse_general_search_form(body: bytes) -> _CamdenSearchForm:
    soup = BeautifulSoup(body, "html.parser")
    forms = soup.select("form#M3Form")
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_discovery_parse("GeneralSearch form")
    form = forms[0]
    if str(form.get("method", "get")).casefold() != "post":
        _raise_discovery_parse("GeneralSearch POST method")
    action = _validated_form_action(str(form.get("action", "")))
    controls = _successful_controls(form, activated_submit="csbtnSearch")
    _require_form_options(form)
    return _CamdenSearchForm(action=HttpUrl(action), controls=controls)


def _successful_controls(  # noqa: C901 - HTML successful controls are one rule set.
    form: Tag,
    *,
    activated_submit: str,
) -> tuple[FormField, ...]:
    fields: list[FormField] = []
    for control in form.select("input, select, textarea, button"):
        if not isinstance(control, Tag) or control.has_attr("disabled"):
            continue
        name = str(control.get("name", ""))
        if not name:
            continue
        tag_name = control.name.casefold()
        control_type = str(control.get("type", "")).casefold()
        if tag_name == "input":
            if control_type in {"checkbox", "radio"} and not control.has_attr(
                "checked"
            ):
                continue
            if control_type in {"button", "file", "image", "reset"}:
                continue
            if control_type == "submit" and name != activated_submit:
                continue
            fields.append(FormField(name=name, value=str(control.get("value", ""))))
        elif tag_name == "select":
            options = [
                option
                for option in control.select("option")
                if isinstance(option, Tag) and not option.has_attr("disabled")
            ]
            selected = [option for option in options if option.has_attr("selected")]
            if not selected and options and not control.has_attr("multiple"):
                selected = options[:1]
            fields.extend(
                FormField(name=name, value=str(option.get("value", "")))
                for option in selected
            )
        elif tag_name == "textarea":
            fields.append(FormField(name=name, value=control.get_text()))
        elif control_type in {"", "submit"} and name == activated_submit:
            value = str(control.get("value", control.get_text(" ", strip=True)))
            fields.append(FormField(name=name, value=value))
    return tuple(fields)


def _search_request(
    form: _CamdenSearchForm,
    query: CamdenDiscoveryQueryV1,
) -> PortalRequest:
    blank = {
        "txtApplicationNumber": "",
        "txtApplicantName": "",
        "txtAgentName": "",
        "txtSiteAddress": "",
        "cboStreetReferenceNumber": "",
        "txtProposal": "",
        "cboWardCode": "",
        "cboApplicationTypeCode": "",
        "cboDevelopmentTypeCode": "",
        "edrDateSelection": "",
    }
    if isinstance(query, CamdenDateQueryV1):
        overrides = {
            **blank,
            "cboStatusCode": "",
            "cboSelectDateValue": str(query.field),
            "rbGroup": "rbRange",
            "dateStart": query.start.strftime("%d-%m-%Y"),
            "dateEnd": query.end.strftime("%d-%m-%Y"),
        }
    else:
        overrides = {
            **blank,
            "cboStatusCode": str(query.status),
            "cboSelectDateValue": str(CamdenDateField.RECEIVED),
            "rbGroup": "rbNotApplicable",
            "dateStart": "",
            "dateEnd": "",
        }
    counts = dict.fromkeys(_OVERRIDDEN_CONTROLS, 0)
    fields: list[FormField] = []
    for field in form.controls:
        if field.name in overrides:
            counts[field.name] += 1
            fields.append(FormField(name=field.name, value=overrides[field.name]))
        else:
            fields.append(field)
    invalid = tuple(name for name, count in counts.items() if count != 1)
    if invalid:
        _raise_discovery_parse(f"unique form controls {', '.join(sorted(invalid))}")
    return PortalRequest(
        url=form.action,
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=tuple(fields),
    )


def _parse_result_page(body: bytes, *, requested_offset: int) -> _CamdenResultPage:
    soup = BeautifulSoup(body, "html.parser")
    text = " ".join(soup.get_text(" ", strip=True).split())
    tables = soup.select('table[summary="Results of the Search"]')
    if _EMPTY_RESULTS in text:
        if requested_offset != 0 or tables:
            _raise_discovery_parse("empty result shape")
        return _CamdenResultPage(reported_count=0, references=(), next_url=None)
    markers = soup.select("#lblPagePosition")
    if len(markers) != 1:
        _raise_discovery_parse("result position marker")
    marker = " ".join(markers[0].get_text(" ", strip=True).split())
    match = re.fullmatch(
        r"Records?\s+(\d+)(?:\s+to\s+(\d+))?\s+of\s+(\d+)",
        marker,
        re.IGNORECASE,
    )
    if match is None:
        _raise_discovery_parse("result position marker")
    first = int(match.group(1))
    last = int(match.group(2) or match.group(1))
    total = int(match.group(3))
    expected_last = min(requested_offset + _RESULT_PAGE_SIZE, total)
    if first != requested_offset + 1 or last != expected_last:
        _raise_discovery_parse("page range")
    if len(tables) != 1:
        _raise_discovery_parse("result table")
    references = _result_references(tables[0])
    if len(references) != last - first + 1:
        _raise_discovery_parse("result row count")
    next_url = _next_result_url(
        soup,
        requested_offset=requested_offset,
        expected_next=last if last < total else None,
    )
    return _CamdenResultPage(
        reported_count=total,
        references=references,
        next_url=next_url,
    )


def _validated_form_action(action: str) -> str:
    resolved = urljoin(GENERAL_SEARCH_URL, action or GENERAL_SEARCH_URL)
    parts = urlsplit(resolved)
    if (
        parts.scheme != "https"
        or parts.hostname != "planningrecords.camden.gov.uk"
        or parts.path not in _GENERAL_SEARCH_PATHS
        or parts.query
        or parts.fragment
    ):
        _raise_discovery_parse("GeneralSearch form action")
    return resolved


def _require_form_options(form: Tag) -> None:
    required = {
        "cboStatusCode": {"", "4", "14"},
        "cboSelectDateValue": {str(field) for field in CamdenDateField},
    }
    for name, required_values in required.items():
        controls = form.select(f'select[name="{name}"]')
        if len(controls) != 1:
            _raise_discovery_parse(f"unique {name} select")
        values = {
            str(option.get("value", "")) for option in controls[0].select("option")
        }
        if not required_values.issubset(values):
            _raise_discovery_parse(f"{name} options")


def _result_references(table: Tag) -> tuple[CamdenSeenReferenceV1, ...]:
    references: list[CamdenSeenReferenceV1] = []
    for row in table.select("tr"):
        if row.select_one("th") is not None:
            continue
        link = row.select_one('td[title="View Application Details"] a[href]')
        if not isinstance(link, Tag):
            _raise_discovery_parse("result detail link")
        reference = link.get_text(" ", strip=True)
        normalized = _normalize_portal_href(
            str(link.get("href", "")),
            base="https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/Generic/",
        )
        parts = urlsplit(normalized)
        parameters = parse_qsl(parts.query, keep_blank_values=True)
        locators = [value for name, value in parameters if name == "PARAM0"]
        if (
            not reference
            or parts.hostname != "planningrecords.camden.gov.uk"
            or not parts.path.endswith("/Generic/StdDetails.aspx")
            or len(locators) != 1
            or not locators[0].isdigit()
        ):
            _raise_discovery_parse("result reference and PARAM0")
        references.append(
            CamdenSeenReferenceV1(reference=reference, locator=locators[0])
        )
    _require_unique_references(tuple(references), context="result page")
    return tuple(references)


def _next_result_url(
    soup: BeautifulSoup,
    *,
    requested_offset: int,
    expected_next: int | None,
) -> HttpUrl | None:
    candidates: dict[int, set[str]] = {}
    for link in soup.select('a[href*="StdResults.aspx"]'):
        normalized = _normalize_portal_href(
            str(link.get("href", "")),
            base="https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/Generic/",
        )
        parts = urlsplit(normalized)
        parameters = parse_qsl(parts.query, keep_blank_values=True)
        offsets = [value for name, value in parameters if name == "p"]
        if len(offsets) != 1 or not offsets[0].isdigit():
            continue
        offset = int(offsets[0])
        candidates.setdefault(offset, set()).add(normalized)
    if expected_next is None:
        if any(offset > requested_offset for offset in candidates):
            _raise_discovery_parse("terminal pager")
        return None
    targets = candidates.get(expected_next, set())
    if len(targets) != 1:
        _raise_discovery_parse("forward pager")
    target = next(iter(targets))
    parameters = parse_qsl(urlsplit(target).query, keep_blank_values=True)
    tokens = [value for name, value in parameters if name == "XMLLoc"]
    page_sizes = [value for name, value in parameters if name == "PS"]
    if len(tokens) != 1 or not tokens[0] or page_sizes != [str(_RESULT_PAGE_SIZE)]:
        _raise_discovery_parse("forward pager session")
    return HttpUrl(target)


def _normalize_portal_href(href: str, *, base: str) -> str:
    resolved = urljoin(base, href)
    parts = urlsplit(resolved)
    parameters = tuple(
        (name.strip(), value.strip())
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(parameters), "")
    )


class CamdenDiscoveryParseError(ValueError):
    """A Camden GeneralSearch boundary did not match its captured contract."""


def _raise_discovery_parse(field: str) -> NoReturn:
    message = f"invalid Camden {field}"
    raise CamdenDiscoveryParseError(message)
