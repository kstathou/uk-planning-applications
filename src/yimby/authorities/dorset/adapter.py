# Copyright (c) 2026 Kostas Stathoulopoulos

"""Dorset Explorer fixtures and Dorset Council live planning discovery."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import ConfigDict, Field, HttpUrl, RootModel

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
    LiveReadiness,
    LiveStatus,
    LiveTransportKind,
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
from yimby.geo import bng_to_wgs84
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
_DISCLAIMER_PATH = "/disclaimer.aspx"
_ACCEPT_BUTTON = "ctl00$ContentPlaceHolder1$btnAccept"
_RECEIVED_FROM = "ctl00$ContentPlaceHolder1$txtDateReceivedFrom"
_RECEIVED_TO = "ctl00$ContentPlaceHolder1$txtDateReceivedTo"
_OUTSTANDING = "ctl00$ContentPlaceHolder1$chkOutstanding"
_NEXT_BUTTON = "ctl00$ContentPlaceHolder1$lvResults$RadDataPager1$ctl02$NextButton"
_BOTTOM_NEXT_BUTTON = "ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton"
_FIRST_PAGED_RESULT = 2
_RESULTS_PER_PAGE = 10
_MARKER_COUNT = 2
_DOCUMENT_CELL_COUNT = 2
_FORM_STATE_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
_DATE_INPUT_STATE_SUFFIX = "_dateInput_ClientState"
_NUMERIC_STATE_SUFFIXES = ("_txtEasting_ClientState", "_txtNorthing_ClientState")


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
    active_new_references: tuple[SourceReference, ...] = ()
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


class DorsetExplorerApplicationV1(FrozenModel):
    """Versioned native payload for the legacy Explorer fixture."""

    kind: Literal["explorer-v1"] = "explorer-v1"
    esri_object_id: int
    application_reference: str
    proposal_description: str
    decision_status: str
    ward_name: str


class DorsetDocumentV1(FrozenModel):
    """One published document row without attachment content."""

    published_date: date
    title: str
    size: str
    url: HttpUrl


class DorsetLiveApplicationV1(FrozenModel):
    """Versioned native payload for a Dorset Council detail page."""

    kind: Literal["live-v1"] = "live-v1"
    application_reference: str
    recno: str
    status: str
    application_type: str
    proposal: str
    validated_date: date
    decision: str | None
    authority: str | None
    address: str
    easting: float
    northing: float
    ward: str | None
    parish: str | None
    documents: tuple[DorsetDocumentV1, ...]
    source_url: HttpUrl


class DorsetApplicationV1(
    RootModel[
        Annotated[
            DorsetExplorerApplicationV1 | DorsetLiveApplicationV1,
            Field(discriminator="kind"),
        ]
    ]
):
    """A retained Dorset payload that preserves its source-specific version."""

    model_config = ConfigDict(frozen=True)


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
        live_status=LiveStatus(
            readiness=LiveReadiness.DISCOVERY_ONLY,
            reason=(
                "same-day live bootstrap passed; two weekly refresh cycles "
                "remain pending"
            ),
            evidence=(
                ".yimby/qualification-dorset-2026-09-16/dorset-qualification-v1.json",
                "weekly cycles due 2026-09-23 and 2026-09-30",
            ),
            transport=LiveTransportKind.HTTP,
        ),
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

    async def _discover_live(
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

        query_keys = tuple(
            query.key for query in _LIVE_QUERIES if window.include_open
        ) or ("received-valid",)
        if (
            progress.active_query is not None
            and progress.active_query not in query_keys
        ):
            _raise_checkpoint("active query")
        _validate_progress(progress, query_keys)

        for query in _LIVE_QUERIES:
            if query.key not in query_keys or query.key in progress.completed_queries:
                continue
            advanced_form = await _load_advanced_form(session)
            result = await session.fetch(_advanced_request(advanced_form, query, scope))
            page = _parse_result_page(result.body)
            if progress.active_query == query.key:
                page = await _replay_active_query(session, progress, page)

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
        """Read an Explorer fixture or a consent-gated live detail page."""
        if session.mode != TransportMode.FIXTURE:
            return await self._fetch_live(session, reference)
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = DorsetApplicationV1(
            DorsetExplorerApplicationV1(
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

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DorsetApplicationV1]:
        locator = reference.locator
        if locator is None or re.fullmatch(r"\d+", locator) is None:
            _raise_parse("detail recno")
        detail_url = f"{LIVE_BASE_URL}/plandisp.aspx?recno={quote(locator, safe='')}"
        request = PortalRequest(url=HttpUrl(detail_url), intent=RequestIntent.DETAIL)
        detail = await session.fetch(request)
        if _is_disclaimer(detail.body):
            disclaimer_form = _parse_disclaimer_form(detail.body)
            await session.fetch(_accept_disclaimer_request(disclaimer_form))
            detail = await session.fetch(request)
        capture = detail.model_copy(update={"url": HttpUrl(detail_url)})
        payload = _parse_live_detail(capture.body, reference, capture.url)
        comments = UnavailableSection(
            reason="public comment text is not exposed by the Dorset register"
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=DorsetApplicationV1(payload),
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(payload.documents)),
                comments=comments,
            ),
            evidence=(capture,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[DorsetApplicationV1],
    ) -> NormalisedObservation:
        """Map Dorset feature attributes to the common record."""
        payload = snapshot.payload.root
        evidence = snapshot.evidence[0].digest
        if isinstance(payload, DorsetLiveApplicationV1):
            return NormalisedObservation(
                authority_id=self.manifest.id,
                reference=snapshot.reference,
                proposal=payload.proposal,
                status=payload.status.casefold().replace(" ", "-"),
                documents=tuple(
                    DocumentRecord(
                        title=(
                            f"{document.published_date.strftime('%d/%m/%Y')} - "
                            f"{document.title} ({document.size})"
                        ),
                        url=document.url,
                    )
                    for document in payload.documents
                ),
                comments=(),
                completeness=snapshot.completeness,
                provenance=(
                    Provenance(field="proposal", evidence=evidence),
                    Provenance(field="status", evidence=evidence),
                ),
                normaliser_version="dorset-v2",
                metadata=ApplicationMetadata(
                    application_type=payload.application_type,
                    decision=payload.decision,
                    address=payload.address,
                    validated_date=payload.validated_date,
                    location=bng_to_wgs84(payload.easting, payload.northing),
                    source_url=payload.source_url,
                ),
            )
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_description,
            status=payload.decision_status.casefold().replace(" ", "-"),
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
    active_references = {
        (reference.reference, reference.locator)
        for reference in progress.active_references
    }
    if any(
        (reference.reference, reference.locator) in active_references
        for reference in page.references
    ):
        _raise_checkpoint("repeated page reference")
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
                "active_new_references": (),
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
                "active_new_references": (*progress.active_new_references, *fresh),
                "seen_references": tuple(seen.values()),
            }
        )
    query_references = (*progress.active_new_references, *fresh) if terminal else ()
    return checkpoint, query_references, terminal


def _validate_progress(
    progress: DorsetCheckpointV1,
    query_keys: tuple[str, ...],
) -> None:
    completed = progress.completed_queries
    if completed != query_keys[: len(completed)] or len(set(completed)) != len(
        completed
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
            or progress.active_new_references
        ):
            _raise_checkpoint("inactive state")
        return
    if (
        len(completed) == len(query_keys)
        or progress.active_query != query_keys[len(completed)]
    ):
        _raise_checkpoint("active query order")


async def _replay_active_query(
    session: PortalSession,
    progress: DorsetCheckpointV1,
    page: _ResultPage,
) -> _ResultPage:
    if progress.next_page < _FIRST_PAGED_RESULT or progress.total_pages is None:
        _raise_checkpoint("active page")
    if len(progress.active_references) != (progress.next_page - 1) * _RESULTS_PER_PAGE:
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
    if str(form.get("method", "")).casefold() != "post" or not _is_disclaimer_action(
        _form_action(form)
    ):
        _raise_parse("disclaimer form")
    fields = _successful_controls(form)
    _require_fields(fields, *_FORM_STATE_FIELDS)
    _require_hidden_inputs(form, *_FORM_STATE_FIELDS)
    if any(field.name in _FORM_STATE_FIELDS and not field.value for field in fields):
        _raise_parse("disclaimer form state")
    _submit_value(form, _ACCEPT_BUTTON, "Accept")
    return form


async def _load_advanced_form(session: PortalSession) -> Tag:
    capture = await session.fetch(
        PortalRequest(url=HttpUrl(_ADVANCED_URL), intent=RequestIntent.SEARCH)
    )
    if _is_disclaimer(capture.body):
        disclaimer_form = _parse_disclaimer_form(capture.body)
        await session.fetch(_accept_disclaimer_request(disclaimer_form))
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(_ADVANCED_URL), intent=RequestIntent.SEARCH)
        )
    return _parse_advanced_form(capture.body)


def _is_disclaimer(body: bytes) -> bool:
    soup = BeautifulSoup(body, "html.parser")
    return any(
        _is_disclaimer_action(_form_action(form))
        for form in soup.select("form")
        if isinstance(form, Tag)
    )


def _is_disclaimer_action(action: str) -> bool:
    parsed = urlsplit(action)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return_url = query.get("returnURL", ())
    return (
        parsed.scheme == "https"
        and parsed.netloc == urlsplit(LIVE_BASE_URL).netloc
        and parsed.path == _DISCLAIMER_PATH
        and set(query) == {"returnURL"}
        and len(return_url) == 1
        and return_url[0].startswith("/")
        and not return_url[0].startswith("//")
    )


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
        *_FORM_STATE_FIELDS,
        _RECEIVED_FROM,
        f"{_RECEIVED_FROM}$dateInput",
        _RECEIVED_TO,
        f"{_RECEIVED_TO}$dateInput",
    )
    _require_hidden_inputs(form, *_FORM_STATE_FIELDS)
    if any(field.name in _FORM_STATE_FIELDS and not field.value for field in fields):
        _raise_parse("advanced form viewstate")
    for query in _LIVE_QUERIES:
        _submit_value(form, query.submit_name, "Search")
    return form


def _parse_result_page(body: bytes) -> _ResultPage:  # noqa: C901
    form = _single_form(body, "result form")
    if (
        str(form.get("method", "")).casefold() != "post"
        or _form_action(form) != _RESULTS_URL
    ):
        _raise_parse("result form")
    fields = _successful_controls(form, frozenset({_NEXT_BUTTON}))
    _require_fields(fields, *_FORM_STATE_FIELDS)
    _require_hidden_inputs(form, *_FORM_STATE_FIELDS)
    if any(field.name in _FORM_STATE_FIELDS and not field.value for field in fields):
        _raise_parse("result form viewstate")
    page, total_pages = _page_markers(form)
    references = []
    result_references = set()
    result_locators = set()
    for link in form.select('a[id$="_hypDisplayRecord"][href]'):
        values = parse_qs(urlsplit(str(link.get("href", ""))).query).get("recno", ())
        if len(values) != 1 or re.fullmatch(r"\d+", values[0]) is None:
            _raise_parse("result recno")
        reference = link.get_text(" ", strip=True)
        if not reference:
            _raise_parse("result reference")
        if reference in result_references or values[0] in result_locators:
            _raise_parse("duplicate result")
        result_references.add(reference)
        result_locators.add(values[0])
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
    bottom_next_controls = form.select(
        f'input[type="submit"][name="{_BOTTOM_NEXT_BUTTON}"]'
    )
    next_allowed = page < total_pages
    if next_allowed and (
        len(next_controls) != 1 or str(next_controls[0].get("value", "")) != " "
    ):
        _raise_parse("next page")
    if bottom_next_controls and (
        len(bottom_next_controls) != 1
        or str(bottom_next_controls[0].get("value", "")) != " "
    ):
        _raise_parse("next page")
    return _ResultPage(
        references=tuple(references),
        page=page,
        total_pages=total_pages,
        form=fields,
        next_allowed=next_allowed,
    )


def _parse_live_detail(
    body: bytes,
    reference: SourceReference,
    source_url: HttpUrl,
) -> DorsetLiveApplicationV1:
    soup = BeautifulSoup(body, "html.parser")
    details = _labelled_detail_values(
        soup,
        "ctl00_ContentPlaceHolder1_pvDetails",
        (
            "Application No",
            "Status",
            "Type",
            "Proposal",
            "Valid Date",
            "Decision",
            "Authority",
        ),
    )
    location = _labelled_detail_values(
        soup,
        "ctl00_ContentPlaceHolder1_pvLocation",
        ("Address", "Easting", "Northing", "Ward", "Parish"),
    )
    locator = reference.locator
    if locator is None or details["Application No"] != reference.reference:
        _raise_parse("detail reference")
    authority = details["Authority"] or None
    if authority not in {None, "Dorset Council"}:
        _raise_parse("detail authority")
    return DorsetLiveApplicationV1(
        application_reference=details["Application No"],
        recno=locator,
        status=_nonempty_value(details, "Status"),
        application_type=_nonempty_value(details, "Type"),
        proposal=_nonempty_value(details, "Proposal"),
        validated_date=_parse_dorset_date(_nonempty_value(details, "Valid Date")),
        decision=details["Decision"] or None,
        authority=authority,
        address=_nonempty_value(location, "Address"),
        easting=_parse_coordinate(_nonempty_value(location, "Easting"), "Easting"),
        northing=_parse_coordinate(_nonempty_value(location, "Northing"), "Northing"),
        ward=location["Ward"] or None,
        parish=location["Parish"] or None,
        documents=_parse_documents_grid(soup, source_url),
        source_url=source_url,
    )


def _labelled_detail_values(
    soup: BeautifulSoup,
    identifier: str,
    required_labels: tuple[str, ...],
) -> dict[str, str]:
    section = soup.select_one(f"#{identifier}")
    if not isinstance(section, Tag):
        _raise_parse(identifier)
    values = {}
    for label in section.select("span.applabel"):
        label_text = label.get_text(" ", strip=True)
        data = label.find_next_sibling("p", class_="appdata")
        if not isinstance(data, Tag) or label_text in values:
            _raise_parse("detail label")
        values[label_text] = data.get_text(" ", strip=True)
    if not set(required_labels).issubset(values):
        _raise_parse("detail label")
    return values


def _nonempty_value(values: dict[str, str], label: str) -> str:
    value = values[label]
    if not value:
        _raise_parse(f"detail {label}")
    return value


def _parse_dorset_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d/%m/%Y").replace(tzinfo=UTC).date()
    except ValueError:
        _raise_parse("detail date")


def _parse_coordinate(value: str, label: str) -> float:
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value) is None:
        _raise_parse(f"detail {label}")
    return float(value)


def _parse_documents_grid(
    soup: BeautifulSoup,
    source_url: HttpUrl,
) -> tuple[DorsetDocumentV1, ...]:
    table = soup.select_one("#ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00")
    if not isinstance(table, Tag):
        _raise_parse("document grid")
    rows = table.select("tbody tr")
    if len(rows) == 1 and rows[0].get("class") == ["rgNoRecords"]:
        cells = rows[0].find_all("td", recursive=False)
        if (
            len(cells) != 1
            or str(cells[0].get("colspan", "")) != "2"
            or cells[0].select("a[href]")
            or cells[0].get_text(" ", strip=True)
            != "There are currently no scanned documents for this application."
        ):
            _raise_parse("document grid")
        _assert_document_grid_proof(soup, 0)
        return ()
    documents = []
    indices = []
    prefix = "ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00__"
    for row in rows:
        row_id = str(row.get("id", ""))
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", row_id)
        links = row.select("a[href]")
        cells = row.find_all("td", recursive=False)
        if match is None or len(links) != 1 or len(cells) != _DOCUMENT_CELL_COUNT:
            _raise_parse("document grid")
        link = links[0]
        index = int(match.group(1))
        onclick = str(link.get("onclick", ""))
        if (
            str(link.get("href", "")) != "#"
            or re.fullmatch(r"\s*return\s+RowClicked\((\d+)\);\s*", onclick) is None
        ):
            _raise_parse("document grid")
        onclick_match = re.fullmatch(
            r"\s*return\s+RowClicked\((\d+)\);\s*",
            onclick,
        )
        if onclick_match is None or int(onclick_match.group(1)) != index:
            _raise_parse("document grid")
        rendered = link.get_text(" ", strip=True)
        published, separator, title = rendered.partition(" - ")
        size_match = re.search(r"\(([^()]+)\)\s*$", row.get_text(" ", strip=True))
        if not separator or not title or size_match is None:
            _raise_parse("document grid")
        documents.append(
            DorsetDocumentV1(
                published_date=_parse_dorset_date(published),
                title=" ".join(title.split()),
                size=size_match.group(1),
                url=HttpUrl(f"{source_url}#document-{index}"),
            )
        )
        indices.append(index)
    if indices != list(range(len(indices))):
        _raise_parse("document grid")
    _assert_document_grid_proof(soup, len(documents))
    return tuple(documents)


def _assert_document_grid_proof(soup: BeautifulSoup, count: int) -> None:
    proofs: list[tuple[bool, int, int]] = []
    for script in soup.select("script"):
        text = script.get_text()
        encoded = re.search(
            r'"_gridTableViewsData"\s*:\s*"((?:\\.|[^"\\])*)"',
            text,
        )
        direct = re.search(
            r'"_gridTableViewsData"\s*:\s*(\[\s*\{.*?\}\s*\])',
            text,
        )
        try:
            if encoded is not None:
                payload = json.loads(json.loads(f'"{encoded.group(1)}"'))
            elif direct is not None:
                payload = json.loads(direct.group(1))
            else:
                continue
        except (json.JSONDecodeError, TypeError):
            _raise_parse("document grid")
        if not isinstance(payload, list) or len(payload) != 1:
            _raise_parse("document grid")
        proof = payload[0]
        if not isinstance(proof, dict):
            _raise_parse("document grid")
        allow_paging = proof.get("AllowPaging")
        page_count = proof.get("PageCount")
        virtual_count = proof.get("VirtualItemCount")
        if (
            not isinstance(allow_paging, bool)
            or not isinstance(page_count, int)
            or not isinstance(virtual_count, int)
        ):
            _raise_parse("document grid")
        proofs.append((allow_paging, page_count, virtual_count))
    if proofs != [(False, 1, count)]:
        _raise_parse("document grid")


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
                if input_type == "submit" and name in include_names:
                    fields.append(
                        FormField(name=name, value=str(control.get("value", "")))
                    )
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
    action = _form_action(form)
    if not _is_disclaimer_action(action):
        _raise_parse("disclaimer form")
    return PortalRequest(
        url=HttpUrl(action),
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
    controls = _successful_controls(
        form,
        frozenset((*overrides, query.submit_name)),
    )
    fields = _override_fields(controls, overrides)
    fields = _override_fields(fields, _telerik_client_state(fields))
    return PortalRequest(
        url=HttpUrl(_ADVANCED_URL),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=(
            FormField(name="__EVENTTARGET", value=""),
            FormField(name="__EVENTARGUMENT", value=""),
            *fields,
        ),
    )


def _telerik_client_state(fields: tuple[FormField, ...]) -> dict[str, str]:
    values = {field.name: field.value for field in fields}
    updates = {}
    for field in fields:
        if field.name.endswith(_NUMERIC_STATE_SUFFIXES):
            updates[field.name] = json.dumps(
                {
                    "enabled": True,
                    "emptyMessage": "",
                    "validationText": "",
                    "valueAsString": "",
                    "minValue": -70_368_744_177_664,
                    "maxValue": 70_368_744_177_664,
                    "lastSetTextBoxValue": "",
                },
                separators=(",", ":"),
            )
            continue
        if not field.name.endswith(_DATE_INPUT_STATE_SUFFIX):
            continue
        client_id = field.name.removesuffix(_DATE_INPUT_STATE_SUFFIX)
        visible_name = f"{client_id.replace('_', '$')}$dateInput"
        base_name = visible_name.removesuffix("$dateInput")
        iso_value = values.get(base_name)
        display_value = values.get(visible_name)
        if (
            iso_value is None
            or display_value is None
            or bool(iso_value) != bool(display_value)
        ):
            _raise_parse("advanced Telerik state")
        validation = f"{iso_value}-00-00-00" if iso_value else ""
        if iso_value and display_value != date.fromisoformat(iso_value).strftime(
            "%d/%m/%Y"
        ):
            _raise_parse("advanced Telerik state")
        updates[field.name] = json.dumps(
            {
                "enabled": True,
                "emptyMessage": "",
                "validationText": validation,
                "valueAsString": validation,
                "minDateStr": "1980-01-01-00-00-00",
                "maxDateStr": "2099-12-31-00-00-00",
                "lastSetTextBoxValue": display_value,
            },
            separators=(",", ":"),
        )
    return updates


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
        form=(
            FormField(name="__EVENTTARGET", value=""),
            FormField(name="__EVENTARGUMENT", value=""),
            *page.form,
        ),
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
