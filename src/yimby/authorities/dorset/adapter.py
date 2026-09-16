# Copyright (c) 2026 Kostas Stathoulopoulos

"""Dorset Explorer fixtures and Dorset Council live planning discovery."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl

from yimby.domain import (
    AuthorityCapabilities,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CapabilityState,
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

SOURCE = SourceId("dorset-explorer-esri")
LIVE_SOURCE = SourceId("dorset-planning-register")
BASE_URL = "https://gi.dorsetcouncil.gov.uk/dorsetexplorer/planning/public"
LIVE_BASE_URL = "https://planning.dorsetcouncil.gov.uk"
_ADVANCED_URL = f"{LIVE_BASE_URL}/advsearch.aspx"
_RESULTS_URL = f"{LIVE_BASE_URL}/searchresults.aspx"
_DISCLAIMER_URL = f"{LIVE_BASE_URL}/disclaimer.aspx?returnURL=%2f"
_ACCEPT_BUTTON = "ctl00$ContentPlaceHolder1$btnAccept"
_RECEIVED_FROM = "ctl00$ContentPlaceHolder1$txtDateReceivedFrom"
_RECEIVED_TO = "ctl00$ContentPlaceHolder1$txtDateReceivedTo"
_OUTSTANDING = "ctl00$ContentPlaceHolder1$chkOutstanding"
_NEXT_BUTTON = "ctl00$ContentPlaceHolder1$lvResults$RadDataPager1$ctl02$NextButton"
_FIRST_PAGED_RESULT = 2
_RESULTS_PER_PAGE = 10
_MARKER_COUNT = 2


class DorsetDiscoveryScope(FrozenModel):
    """Exact live discovery request represented by a checkpoint."""

    start: date
    end: date
    include_open: bool


class DorsetCheckpointV1(FrozenModel):
    """Fixture offset or resumable Dorset Council live discovery state."""

    object_offset: str = "0"
    live_scope: DorsetDiscoveryScope | None = None
    completed_queries: tuple[str, ...] = ()
    active_query: str | None = None
    next_page: int = 1
    total_pages: int | None = None
    active_references: tuple[SourceReference, ...] = ()
    seen_references: tuple[SourceReference, ...] = ()
    live_complete: bool = False


class _LiveQuery(FrozenModel):
    key: Literal["received-valid", "outstanding"]
    submit_name: str


_LIVE_QUERIES = (
    _LiveQuery(
        key="received-valid",
        submit_name="ctl00$ContentPlaceHolder1$btnSearch3",
    ),
    _LiveQuery(
        key="outstanding",
        submit_name="ctl00$ContentPlaceHolder1$btnSearch2",
    ),
)


class _ResultPage(FrozenModel):
    references: tuple[SourceReference, ...]
    page: int
    total_pages: int
    form: tuple[FormField, ...]
    next_allowed: bool


class DorsetApplicationV1(FrozenModel):
    """Dorset-native Esri planning feature."""

    esri_object_id: int
    application_reference: str
    proposal_description: str
    decision_status: str
    ward_name: str


class DorsetParseError(ValueError):
    """A required Dorset portal value was absent or inconsistent."""

    def __init__(self, field: str) -> None:
        """Name the invalid portal field."""
        super().__init__(f"missing Dorset field {field}")


class DorsetCheckpointError(ValueError):
    """Saved Dorset progress cannot safely resume against the current portal."""

    def __init__(self, detail: str) -> None:
        """Name the checkpoint invariant that failed."""
        super().__init__(f"invalid Dorset checkpoint {detail}")


class DorsetAdapter:
    """Keep Dorset fixture and live register vocabulary local."""

    manifest = AuthorityManifest(
        id=AuthorityId("dorset"),
        name="Dorset Council",
        kind=AuthorityKind.UNITARY,
        sources=(
            SourceDefinition(id=LIVE_SOURCE, base_url=HttpUrl(f"{LIVE_BASE_URL}/")),
        ),
        capabilities=AuthorityCapabilities(discovery=CapabilityState.UNKNOWN),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DorsetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DorsetCheckpointV1]]:
        """Use Explorer fixtures or the captured Dorset Council live flow."""
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
        checkpoint: DorsetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DorsetCheckpointV1]]:
        offset = "0" if checkpoint is None else checkpoint.object_offset
        url = (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&offset={quote(offset)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-dorset-reference="([^"]+)"', html)
        )
        next_offset = _required(
            html,
            r'data-dorset-offset="([^"]+)"',
            "offset",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=DorsetCheckpointV1(object_offset=next_offset),
            complete=next_offset == "complete",
        )

    async def _discover_live(  # noqa: C901
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DorsetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DorsetCheckpointV1]]:
        scope = DorsetDiscoveryScope(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )
        progress = checkpoint
        if progress is None or progress.live_scope != scope:
            progress = DorsetCheckpointV1(object_offset="live", live_scope=scope)
        if progress.live_complete:
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return

        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_ADVANCED_URL), intent=RequestIntent.SEARCH)
        )
        disclaimer_form = _parse_disclaimer_form(form_capture.body)
        await session.fetch(_accept_disclaimer_request(disclaimer_form))
        advanced_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_ADVANCED_URL), intent=RequestIntent.SEARCH)
        )
        advanced_form = _parse_advanced_form(advanced_capture.body)
        query_keys = (
            tuple(query.key for query in _LIVE_QUERIES if window.include_open)
            or ("received-valid",)
        )
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            _raise_checkpoint("active query")
        _validate_progress(progress, query_keys)

        for query in _LIVE_QUERIES:
            if query.key not in query_keys or query.key in progress.completed_queries:
                continue
            result = await session.fetch(_advanced_request(advanced_form, query, scope))
            page = _parse_result_page(result.body)
            if progress.active_query == query.key:
                page = await _replay_active_query(session, progress, page)
            elif progress.active_query is not None:
                _raise_checkpoint("active query order")

            while True:
                next_checkpoint, fresh, complete_query = _advance_checkpoint(
                    progress,
                    query_key=query.key,
                    page=page,
                    query_keys=query_keys,
                )
                yield DiscoveryBatch(
                    references=fresh,
                    next_checkpoint=next_checkpoint,
                    complete=next_checkpoint.live_complete,
                )
                progress = next_checkpoint
                if complete_query:
                    break
                page_capture = await session.fetch(_next_page_request(page))
                page = _parse_result_page(page_capture.body)

        if not progress.live_complete:
            completed = progress.model_copy(update={"live_complete": True})
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=completed,
                complete=True,
            )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DorsetApplicationV1]:
        """Read the existing rendered Explorer fixture detail."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = DorsetApplicationV1(
            esri_object_id=int(
                _required(html, r'data-dorset-object-id="([^"]+)"', "object id")
            ),
            application_reference=reference.reference,
            proposal_description=unescape(
                _required(html, r'data-dorset-proposal="([^"]+)"', "proposal")
            ),
            decision_status=_required(
                html,
                r'data-dorset-status="([^"]+)"',
                "status",
            ),
            ward_name=_required(html, r'data-dorset-ward="([^"]+)"', "ward"),
        )
        unavailable = UnavailableSection(reason="JavaScript map section not verified")
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
        snapshot: NativeSnapshot[DorsetApplicationV1],
    ) -> NormalisedObservation:
        """Map Dorset feature attributes to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_description,
            status=snapshot.payload.decision_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="dorset-v1",
        )


def _advance_checkpoint(
    progress: DorsetCheckpointV1,
    *,
    query_key: str,
    page: _ResultPage,
    query_keys: tuple[str, ...],
) -> tuple[DorsetCheckpointV1, tuple[SourceReference, ...], bool]:
    if progress.active_query == query_key:
        if progress.total_pages != page.total_pages or progress.next_page != page.page:
            _raise_checkpoint("active page")
    elif progress.active_query is not None:
        _raise_checkpoint("active query")
    seen = {reference.reference: reference for reference in progress.seen_references}
    fresh = []
    for reference in page.references:
        prior = seen.get(reference.reference)
        if prior is None:
            seen[reference.reference] = reference
            fresh.append(reference)
        elif prior.locator != reference.locator:
            _raise_checkpoint("duplicate locator")
    terminal = page.page == page.total_pages
    if terminal:
        completed_queries = (*progress.completed_queries, query_key)
        if len(set(completed_queries)) != len(completed_queries):
            _raise_checkpoint("completed query")
        checkpoint = progress.model_copy(
            update={
                "completed_queries": completed_queries,
                "active_query": None,
                "next_page": 1,
                "total_pages": None,
                "active_references": (),
                "seen_references": tuple(seen.values()),
                "live_complete": len(completed_queries) == len(query_keys),
            }
        )
    else:
        checkpoint = progress.model_copy(
            update={
                "active_query": query_key,
                "next_page": page.page + 1,
                "total_pages": page.total_pages,
                "active_references": (*progress.active_references, *page.references),
                "seen_references": tuple(seen.values()),
            }
        )
    return checkpoint, tuple(fresh), terminal


def _validate_progress(
    progress: DorsetCheckpointV1,
    query_keys: tuple[str, ...],
) -> None:
    completed = progress.completed_queries
    if (
        completed != query_keys[: len(completed)]
        or len(set(completed)) != len(completed)
    ):
        _raise_checkpoint("completed queries")
    seen: dict[str, SourceReference] = {}
    for reference in progress.seen_references:
        prior = seen.get(reference.reference)
        if prior is not None and prior.locator != reference.locator:
            _raise_checkpoint("seen locator")
        seen[reference.reference] = reference
    if progress.active_query is None:
        if (
            progress.next_page != 1
            or progress.total_pages is not None
            or progress.active_references
        ):
            _raise_checkpoint("inactive state")
        return
    if len(completed) == len(query_keys) or progress.active_query != query_keys[
        len(completed)
    ]:
        _raise_checkpoint("active query order")


async def _replay_active_query(
    session: PortalSession,
    progress: DorsetCheckpointV1,
    page: _ResultPage,
) -> _ResultPage:
    if progress.next_page < _FIRST_PAGED_RESULT or progress.total_pages is None:
        _raise_checkpoint("active page")
    if len(progress.active_references) != (
        progress.next_page - 1
    ) * _RESULTS_PER_PAGE:
        _raise_checkpoint("active references")
    current = page
    for expected_page in range(1, progress.next_page):
        if current.page != expected_page or current.total_pages != progress.total_pages:
            _raise_checkpoint("replayed page")
        start = (expected_page - 1) * _RESULTS_PER_PAGE
        expected = progress.active_references[start : start + _RESULTS_PER_PAGE]
        if current.references != expected:
            _raise_checkpoint("replayed references")
        if expected_page + 1 < progress.next_page:
            capture = await session.fetch(_next_page_request(current))
            current = _parse_result_page(capture.body)
    capture = await session.fetch(_next_page_request(current))
    return _parse_result_page(capture.body)


def _parse_disclaimer_form(body: bytes) -> Tag:
    form = _single_form(body, "disclaimer form")
    if (
        str(form.get("method", "")).casefold() != "post"
        or _form_action(form) != _DISCLAIMER_URL
    ):
        _raise_parse("disclaimer form")
    fields = _successful_controls(form)
    _require_fields(fields, "__EVENTTARGET", "__VIEWSTATE")
    _require_hidden_inputs(form, "__EVENTTARGET", "__VIEWSTATE")
    _submit_value(form, _ACCEPT_BUTTON, "Accept")
    return form


def _parse_advanced_form(body: bytes) -> Tag:
    form = _single_form(body, "advanced form")
    if (
        str(form.get("method", "")).casefold() != "post"
        or _form_action(form) != _ADVANCED_URL
    ):
        _raise_parse("advanced form")
    fields = _successful_controls(form)
    _require_fields(
        fields,
        "__EVENTTARGET",
        "__EVENTARGUMENT",
        "__VIEWSTATE",
        _RECEIVED_FROM,
        f"{_RECEIVED_FROM}$dateInput",
        _RECEIVED_TO,
        f"{_RECEIVED_TO}$dateInput",
    )
    _require_hidden_inputs(form, "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE")
    if not any(field.name == "__VIEWSTATE" and field.value for field in fields):
        _raise_parse("advanced form viewstate")
    for query in _LIVE_QUERIES:
        _submit_value(form, query.submit_name, "Search")
    return form


def _parse_result_page(body: bytes) -> _ResultPage:
    form = _single_form(body, "result form")
    if (
        str(form.get("method", "")).casefold() != "post"
        or _form_action(form) != _RESULTS_URL
    ):
        _raise_parse("result form")
    fields = _successful_controls(form)
    _require_fields(fields, "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE")
    _require_hidden_inputs(form, "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE")
    page, total_pages = _page_markers(form)
    references = []
    for link in form.select('a[id$="_hypDisplayRecord"][href]'):
        if not isinstance(link, Tag):
            _raise_parse("result link")
        values = parse_qs(urlsplit(str(link.get("href", ""))).query).get("recno", ())
        if len(values) != 1 or re.fullmatch(r"\d+", values[0]) is None:
            _raise_parse("result recno")
        reference = link.get_text(" ", strip=True)
        if not reference:
            _raise_parse("result reference")
        references.append(
            SourceReference(
                source_id=LIVE_SOURCE,
                reference=reference,
                locator=values[0],
            )
        )
    if page < total_pages and len(references) != _RESULTS_PER_PAGE:
        _raise_parse("nonterminal result rows")
    if page == total_pages and not 1 <= len(references) <= _RESULTS_PER_PAGE:
        _raise_parse("terminal result rows")
    next_controls = form.select(f'input[type="submit"][name="{_NEXT_BUTTON}"]')
    next_allowed = page < total_pages
    if next_allowed and (
        len(next_controls) != 1 or str(next_controls[0].get("value", "")) != " "
    ):
        _raise_parse("next page")
    return _ResultPage(
        references=tuple(references),
        page=page,
        total_pages=total_pages,
        form=fields,
        next_allowed=next_allowed,
    )


def _single_form(body: bytes, field: str) -> Tag:
    forms = BeautifulSoup(body, "html.parser").select("form")
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_parse(field)
    return forms[0]


def _form_action(form: Tag) -> str:
    return urljoin(f"{LIVE_BASE_URL}/", str(form.get("action", "")))


def _successful_controls(
    form: Tag,
    include_names: frozenset[str] = frozenset(),
) -> tuple[FormField, ...]:
    fields = []
    for control in form.select("input[name], select[name], textarea[name]"):
        name = control.get("name")
        if not isinstance(name, str) or control.has_attr("disabled"):
            continue
        if control.name == "input":
            input_type = str(control.get("type", "text")).casefold()
            if input_type in {"button", "file", "image", "reset", "submit"}:
                continue
            if (
                input_type in {"checkbox", "radio"}
                and not control.has_attr("checked")
                and name not in include_names
            ):
                continue
            value = str(control.get("value", "on" if input_type == "checkbox" else ""))
            fields.append(FormField(name=name, value=value))
        elif control.name == "select":
            selected = list(control.select("option[selected]"))
            if not selected:
                first = control.select_one("option")
                selected = [] if first is None else [first]
            if not control.has_attr("multiple"):
                selected = selected[:1]
            fields.extend(
                FormField(name=name, value=str(option.get("value", "")))
                for option in selected
            )
        else:
            fields.append(FormField(name=name, value=control.get_text()))
    return tuple(fields)


def _require_fields(fields: tuple[FormField, ...], *names: str) -> None:
    present = {field.name for field in fields}
    if not set(names).issubset(present):
        _raise_parse("form state")


def _require_hidden_inputs(form: Tag, *names: str) -> None:
    hidden_names = {
        name
        for control in form.select('input[type="hidden"][name]')
        if isinstance((name := control.get("name")), str)
    }
    if not set(names).issubset(hidden_names):
        _raise_parse("form state")


def _submit_value(form: Tag, name: str, expected: str) -> str:
    submits = form.select(f'input[type="submit"][name="{name}"]')
    if len(submits) != 1 or str(submits[0].get("value", "")) != expected:
        _raise_parse("form submit")
    return expected


def _accept_disclaimer_request(form: Tag) -> PortalRequest:
    return PortalRequest(
        url=HttpUrl(_DISCLAIMER_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=(
            *_successful_controls(form),
            FormField(name=_ACCEPT_BUTTON, value="Accept"),
        ),
    )


def _advanced_request(
    form: Tag,
    query: _LiveQuery,
    scope: DorsetDiscoveryScope,
) -> PortalRequest:
    if query.key == "received-valid":
        overrides = {
            _RECEIVED_FROM: scope.start.isoformat(),
            f"{_RECEIVED_FROM}$dateInput": scope.start.strftime("%d/%m/%Y"),
            _RECEIVED_TO: scope.end.isoformat(),
            f"{_RECEIVED_TO}$dateInput": scope.end.strftime("%d/%m/%Y"),
        }
    else:
        overrides = {_OUTSTANDING: "on"}
    fields = _override_fields(
        _successful_controls(form, frozenset(overrides)),
        overrides,
    )
    return PortalRequest(
        url=HttpUrl(_ADVANCED_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=(*fields, FormField(name=query.submit_name, value="Search")),
    )


def _override_fields(
    fields: tuple[FormField, ...], values: dict[str, str]
) -> tuple[FormField, ...]:
    replaced = set()
    updated = []
    for field in fields:
        if field.name in values:
            updated.append(FormField(name=field.name, value=values[field.name]))
            replaced.add(field.name)
        else:
            updated.append(field)
    updated.extend(
        FormField(name=name, value=value)
        for name, value in values.items()
        if name not in replaced
    )
    return tuple(updated)


def _page_markers(form: Tag) -> tuple[int, int]:
    markers = []
    for pager in form.select(
        "#ctl00_ContentPlaceHolder1_lvResults_RadDataPager1, "
        "#ctl00_ContentPlaceHolder1_lvResults_pager"
    ):
        text = pager.get_text(" ", strip=True)
        page_matches = re.findall(r"Page\s+([1-9]\d*)\s+of\s+([1-9]\d*)", text)
        current = pager.select(".rdpCurrentPage")
        if len(page_matches) != 1 or len(current) != 1:
            _raise_parse("page markers")
        current_value = current[0].get_text(" ", strip=True)
        if re.fullmatch(r"[1-9]\d*", current_value) is None:
            _raise_parse("current page")
        page, total = (int(value) for value in page_matches[0])
        if int(current_value) != page or page > total:
            _raise_parse("page markers")
        markers.append((page, total))
    if len(markers) != _MARKER_COUNT or markers[0] != markers[1]:
        _raise_parse("page markers")
    return markers[0]


def _next_page_request(page: _ResultPage) -> PortalRequest:
    if not page.next_allowed:
        _raise_parse("next page")
    return PortalRequest(
        url=HttpUrl(_RESULTS_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=(*page.form, FormField(name=_NEXT_BUTTON, value=" ")),
    )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


def _raise_parse(field: str) -> NoReturn:
    raise DorsetParseError(field)


def _raise_checkpoint(detail: str) -> NoReturn:
    raise DorsetCheckpointError(detail)
