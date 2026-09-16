# Copyright (c) 2026 Kostas Stathoulopoulos

"""Arun-owned fixture and live Ocella planning-register adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Literal, NoReturn
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
    FrozenModel,
    NativeDocument,
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
from yimby.transport import FormField, PortalRequest, RequestIntent, RequestMethod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("arun-ocella")
BASE_URL = "https://www1.arun.gov.uk/aplanning/OcellaWeb"
_SEARCH_URL = f"{BASE_URL}/planningSearch"
_DATE_FORMATS = ("%d-%m-%y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y")
_MINIMUM_LABELLED_CELLS = 2
_OPEN_HISTORY_START = date(1948, 1, 1)
_OPEN_ANNUAL_END = date(2023, 12, 31)
_DECEMBER = 12


class ArunDiscoveryScope(FrozenModel):
    """Exact inclusive discovery request owning resumable Arun progress."""

    start: date
    end: date
    include_open: bool


class ArunReceivedQuery(FrozenModel):
    """Applications received inside the requested first-pass window."""

    kind: Literal["received"] = "received"
    start: date
    end: date

    @property
    def key(self) -> str:
        """Return the stable checkpoint and receipt identity."""
        return f"{self.kind}|{self.start.isoformat()}|{self.end.isoformat()}"


class ArunDecidedQuery(FrozenModel):
    """Applications decided inside the requested first-pass window."""

    kind: Literal["decided"] = "decided"
    start: date
    end: date

    @property
    def key(self) -> str:
        """Return the stable checkpoint and receipt identity."""
        return f"{self.kind}|{self.start.isoformat()}|{self.end.isoformat()}"


class ArunOpenReceivedQuery(FrozenModel):
    """Undecided applications received inside one complete partition."""

    kind: Literal["open-received"] = "open-received"
    start: date
    end: date

    @property
    def key(self) -> str:
        """Return the stable checkpoint and receipt identity."""
        return f"{self.kind}|{self.start.isoformat()}|{self.end.isoformat()}"


type ArunQuery = ArunReceivedQuery | ArunDecidedQuery | ArunOpenReceivedQuery


def _canonical_query_plan(scope: ArunDiscoveryScope) -> tuple[ArunQuery, ...]:
    plan: list[ArunQuery] = [
        ArunReceivedQuery(start=scope.start, end=scope.end),
        ArunDecidedQuery(start=scope.start, end=scope.end),
    ]
    if not scope.include_open:
        return tuple(plan)
    plan.append(
        ArunOpenReceivedQuery(
            start=_OPEN_HISTORY_START,
            end=date(1999, 12, 31),
        )
    )
    plan.extend(
        ArunOpenReceivedQuery(
            start=date(year, 1, 1),
            end=date(year, 12, 31),
        )
        for year in range(2000, _OPEN_ANNUAL_END.year + 1)
    )
    cursor = _OPEN_ANNUAL_END + timedelta(days=1)
    while cursor <= scope.end:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == _DECEMBER
            else date(cursor.year, cursor.month + 1, 1)
        )
        plan.append(
            ArunOpenReceivedQuery(
                start=cursor,
                end=min(scope.end, next_month - timedelta(days=1)),
            )
        )
        cursor = next_month
    return tuple(plan)


class ArunCheckpointV1(FrozenModel):
    """Fixture cursor plus resumable Arun received-search progress."""

    result_row: str
    live_phase: Literal["initial", "show-all", "complete"] = "initial"
    window_start: date | None = None
    window_end: date | None = None
    seen_references: tuple[str, ...] = ()


class ArunApplicationV1(FrozenModel):
    """Arun-native Ocella application."""

    ocella_reference: str
    proposal_text: str
    decision_status: str
    parish_name: str
    documents: tuple[NativeDocument, ...]
    site_address: str | None = None
    application_type: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision_date: date | None = None
    case_officer: str | None = None
    applicant: str | None = None
    agent: str | None = None


class ArunAdapter:
    """Own Arun request, parsing, cap, and completeness rules."""

    manifest = AuthorityManifest(
        id=AuthorityId("arun"),
        name="Arun District Council",
        kind=AuthorityKind.DISTRICT,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: ArunCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[ArunCheckpointV1]]:
        """Use fixtures or the bounded received-date Ocella search."""
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
        checkpoint: ArunCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[ArunCheckpointV1]]:
        row = "first" if checkpoint is None else checkpoint.result_row
        url = (
            f"{BASE_URL}/Search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&row={quote(row)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-arun-reference="([^"]+)"', html)
        )
        next_row = _required_fixture(html, r'data-arun-row="([^"]+)"', "result row")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=ArunCheckpointV1(result_row=next_row),
            complete=next_row == "complete",
        )

    async def _discover_live(  # noqa: C901
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: ArunCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[ArunCheckpointV1]]:
        progress = checkpoint or ArunCheckpointV1(result_row="live")
        _assert_window(progress, window)
        if progress.live_phase == "complete":
            if window.include_open:
                raise ArunOpenEnumerationUnsupportedError
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_SEARCH_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_search_form(form_capture.body)
        initial_capture = await session.fetch(
            _search_request(form, window, show_all=False)
        )
        initial = _parse_search_results(initial_capture.body)
        if len(initial.references) > initial.reported:
            raise ArunCountMismatchError(initial.reported, len(initial.references))
        if progress.live_phase == "initial":
            fresh, seen = _fresh(initial.references, progress.seen_references)
            initial_complete = len(initial.references) == initial.reported
            if not initial_complete and not initial.has_show_all:
                raise ArunCountMismatchError(initial.reported, len(initial.references))
            progress = progress.model_copy(
                update={
                    "live_phase": "complete" if initial_complete else "show-all",
                    "window_start": window.start,
                    "window_end": window.end,
                    "seen_references": seen,
                }
            )
            yield DiscoveryBatch(
                references=fresh,
                next_checkpoint=progress,
                complete=initial_complete and not window.include_open,
            )
            if initial_complete:
                if window.include_open:
                    raise ArunOpenEnumerationUnsupportedError
                return
        if not initial.has_show_all:
            raise ArunCountMismatchError(initial.reported, len(initial.references))
        expanded_capture = await session.fetch(
            _search_request(form, window, show_all=True)
        )
        expanded = _parse_search_results(expanded_capture.body)
        if (
            expanded.reported != initial.reported
            or len(expanded.references) != initial.reported
        ):
            raise ArunCountMismatchError(initial.reported, len(expanded.references))
        fresh, seen = _fresh(expanded.references, progress.seen_references)
        completed = progress.model_copy(
            update={
                "live_phase": "complete",
                "window_start": window.start,
                "window_end": window.end,
                "seen_references": seen,
            }
        )
        yield DiscoveryBatch(
            references=fresh,
            next_checkpoint=completed,
            complete=not window.include_open,
        )
        if window.include_open:
            raise ArunOpenEnumerationUnsupportedError

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[ArunApplicationV1]:
        """Read fixture detail or the captured live Ocella summary."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[ArunApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{BASE_URL}/PlanningDetails?reference={encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        documents = tuple(
            NativeDocument(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-arun-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = ArunApplicationV1(
            ocella_reference=reference.reference,
            proposal_text=unescape(
                _required_fixture(html, r'data-arun-proposal="([^"]+)"', "proposal")
            ),
            decision_status=_required_fixture(
                html, r'data-arun-status="([^"]+)"', "status"
            ),
            parish_name=_required_fixture(
                html, r'data-arun-parish="([^"]+)"', "parish"
            ),
            documents=documents,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=UnavailableSection(reason="pdf-only-unavailable"),
            ),
            evidence=(detail,),
        )

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[ArunApplicationV1]:
        if reference.source_id != SOURCE:
            raise ArunRoutingError(reference.reference)
        url = reference.locator or (
            f"{BASE_URL}/planningDetails?reference="
            f"{quote(reference.reference, safe='')}&from=planningSearch"
        )
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        fields = _parse_labelled_fields(detail.body)
        published = _required_field(fields, "reference", "application reference")
        if published != reference.reference:
            raise ArunReferenceMismatchError(reference.reference, published)
        payload = ArunApplicationV1(
            ocella_reference=published,
            proposal_text=_required_field(fields, "proposal", "description"),
            decision_status=_required_field(fields, "status"),
            parish_name=_required_field(fields, "parish"),
            documents=(),
            site_address=_optional_field(fields, "location", "address"),
            application_type=_optional_field(fields, "application type", "type"),
            received_date=_optional_date(fields, "received date"),
            validated_date=_optional_date(fields, "validated date"),
            decision_date=_optional_date(fields, "decision date"),
            case_officer=_optional_field(fields, "case officer"),
            applicant=_optional_field(fields, "applicant"),
            agent=_optional_field(fields, "agent"),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=UnavailableSection(
                    reason="Ocella document action parameter contract is unresolved"
                ),
                comments=UnavailableSection(
                    reason="Ocella undecided comment flow is unresolved"
                ),
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[ArunApplicationV1],
    ) -> NormalisedObservation:
        """Map Arun-native values to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_text,
            status=payload.decision_status.casefold().replace(" ", "-"),
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
            normaliser_version="arun-v2",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                address=payload.site_address,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                published_parties=tuple(
                    value for value in (payload.applicant, payload.agent) if value
                ),
                officer_name=payload.case_officer,
                source_url=snapshot.evidence[0].url,
            ),
        )


class _SearchResults(FrozenModel):
    references: tuple[SourceReference, ...]
    reported: int
    has_show_all: bool


def _parse_search_form(body: bytes) -> Tag:
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one('form[action*="planningSearch"]') or soup.select_one("form")
    if not isinstance(form, Tag):
        _raise_parse("planning search form")
    return form


def _form_fields(form: Tag) -> tuple[FormField, ...]:
    fields = []
    for control in form.select("input[name], select[name], textarea[name]"):
        name = control.get("name")
        if not isinstance(name, str):
            continue
        input_type = str(control.get("type", "text")).casefold()
        if control.name == "input" and input_type in {
            "button",
            "image",
            "reset",
            "submit",
        }:
            continue
        if control.name == "select":
            selected = control.select_one("option[selected]") or control.select_one(
                "option"
            )
            value = "" if selected is None else str(selected.get("value", ""))
        else:
            value = (
                str(control.get("value", ""))
                if control.name == "input"
                else control.get_text(strip=True)
            )
        fields.append(FormField(name=name, value=value))
    return tuple(fields)


def _search_request(
    form: Tag, window: DiscoveryWindow, *, show_all: bool
) -> PortalRequest:
    values = {
        "reference": "",
        "location": "",
        "OcellaPlanningSearch.postcode": "",
        "area": "",
        "applicant": "",
        "agent": "",
        "undecided": "",
        "type": "",
        "receivedFrom": window.start.strftime("%d-%m-%y"),
        "receivedTo": window.end.strftime("%d-%m-%y"),
        "decidedFrom": "",
        "decidedTo": "",
    }
    if show_all:
        values["showall"] = "Show all results"
    existing = _form_fields(form)
    names = {field.name for field in existing}
    fields = tuple(
        FormField(name=field.name, value=values.get(field.name, field.value))
        for field in existing
    ) + tuple(
        FormField(name=name, value=value)
        for name, value in values.items()
        if name not in names
    )
    return PortalRequest(
        url=HttpUrl(urljoin(f"{BASE_URL}/", str(form.get("action", _SEARCH_URL)))),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=fields,
    )


def _parse_search_results(body: bytes) -> _SearchResults:
    soup = BeautifulSoup(body, "html.parser")
    found = []
    seen = set()
    for link in soup.select('a[href*="planningDetails"]'):
        href = str(link.get("href", ""))
        values = parse_qs(urlsplit(href).query).get("reference", [])
        if len(values) != 1 or not values[0]:
            _raise_parse("result reference")
        reference = values[0]
        if reference not in seen:
            seen.add(reference)
            found.append(
                SourceReference(
                    source_id=SOURCE,
                    reference=reference,
                    locator=urljoin(f"{BASE_URL}/", href),
                )
            )
    text = soup.get_text(" ", strip=True)
    count_element = soup.select_one("[data-result-count]")
    match = re.search(r"\b(\d+)\s+(?:records?|results?)\b", text, re.IGNORECASE)
    if isinstance(count_element, Tag):
        reported = int(str(count_element.get("data-result-count")))
    elif match is not None:
        reported = int(match.group(1))
    elif "no records" in text.casefold() or "no results" in text.casefold():
        reported = 0
    else:
        _raise_parse("reported result count")
    controls = (*soup.select("input, button"), *soup.select("button, a"))
    show_all = any(
        "show all"
        in f"{item.get('value', '')} {item.get_text(' ', strip=True)}".casefold()
        for item in controls
    )
    return _SearchResults(
        references=tuple(found), reported=reported, has_show_all=show_all
    )


def _parse_labelled_fields(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= _MINIMUM_LABELLED_CELLS:
            fields[_normalise_label(cells[0].get_text(" ", strip=True))] = cells[
                -1
            ].get_text(" ", strip=True)
    for term in soup.select("dt"):
        value = term.find_next_sibling("dd")
        if isinstance(value, Tag):
            fields[_normalise_label(term.get_text(" ", strip=True))] = value.get_text(
                " ", strip=True
            )
    if not fields:
        _raise_parse("labelled detail fields")
    return fields


def _fresh(
    references: tuple[SourceReference, ...], seen_values: tuple[str, ...]
) -> tuple[tuple[SourceReference, ...], tuple[str, ...]]:
    seen = set(seen_values)
    fresh = []
    for reference in references:
        if reference.reference not in seen:
            seen.add(reference.reference)
            fresh.append(reference)
    return tuple(fresh), tuple(seen)


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


def _assert_window(checkpoint: ArunCheckpointV1, window: DiscoveryWindow) -> None:
    if checkpoint.window_start is not None and (
        checkpoint.window_start != window.start or checkpoint.window_end != window.end
    ):
        raise ArunCheckpointError


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class ArunParseError(ValueError):
    """A required Arun boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field without retaining response content."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Arun field {field}")


class ArunCountMismatchError(ValueError):
    """Ocella's displayed result count did not match enumerated rows."""

    def __init__(self, expected: int, actual: int) -> None:
        """Report only counts."""
        super().__init__(f"Arun reported {expected} results but exposed {actual}")


class ArunCheckpointError(ValueError):
    """A saved cursor belongs to another received-date window."""


class ArunOpenEnumerationUnsupportedError(RuntimeError):
    """Older open applications cannot yet be enumerated completely."""

    def __init__(self) -> None:
        """Keep the unsupported boundary explicit."""
        super().__init__("Arun older-open enumeration is not verified")


class ArunRoutingError(ValueError):
    """A reference belongs to another source."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Arun cannot route reference {reference}")


class ArunReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(f"expected Arun reference {expected}, received {actual}")


def _raise_parse(field: str) -> NoReturn:
    raise ArunParseError(field)
