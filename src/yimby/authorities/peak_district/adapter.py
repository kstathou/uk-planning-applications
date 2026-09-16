# Copyright (c) 2026 Kostas Stathoulopoulos

"""Peak District-owned legacy and AssureLive adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote, urljoin

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
    FailedSection,
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
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from yimby.domain import EvidenceCapture
    from yimby.transport import PortalSession

LEGACY_SOURCE = SourceId("peak-district-legacy")
LEGACY_BASE = "https://portal.peakdistrict.gov.uk"
ASSURE_BASE = "https://planning.peakdistrict.gov.uk/AssureLive"
_WEEKLY_URL = f"{LEGACY_BASE}/quicksearch/validated_past_week"
_DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")
_MINIMUM_LABELLED_CELLS = 2


class PeakDistrictCheckpointV1(FrozenModel):
    """Fixture cursor plus legacy weekly-search completion."""

    row_offset: str
    window_start: date | None = None
    window_end: date | None = None
    seen_references: tuple[str, ...] = ()
    live_complete: bool = False


class PeakDistrictApplicationV1(FrozenModel):
    """Peak District-native legacy summary."""

    park_reference: str
    record_type: str
    proposal_summary: str
    case_status: str
    parish: str
    legacy_record_url: HttpUrl
    development_address: str | None = None
    planning_portal_reference: str | None = None
    validated_date: date | None = None
    assurelive_url: HttpUrl | None = None
    loading_sections: tuple[str, ...] = ()


class PeakDistrictAdapter:
    """Own the legacy weekly and visible-loading-state boundaries."""

    manifest = AuthorityManifest(
        id=AuthorityId("peak-district"),
        name="Peak District National Park Authority",
        kind=AuthorityKind.NATIONAL_PARK,
        sources=(
            SourceDefinition(
                id=LEGACY_SOURCE,
                base_url=HttpUrl(f"{LEGACY_BASE}/"),
            ),
            SourceDefinition(
                id=SourceId("peak-district-assurelive"),
                base_url=HttpUrl(f"{ASSURE_BASE}/"),
                valid_from=date(2025, 1, 1),
            ),
        ),
    )

    def __init__(self, today: Callable[[], date] = date.today) -> None:
        """Inject today's date so the rolling weekly boundary is testable."""
        self._today = today

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        """Use fixtures or enumerate every row in the legacy weekly table."""
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
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        offset = "0" if checkpoint is None else checkpoint.row_offset
        url = (
            f"{LEGACY_BASE}/search?validatedFrom={window.start.isoformat()}"
            f"&validatedTo={window.end.isoformat()}&offset={quote(offset)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=LEGACY_SOURCE, reference=value)
            for value in re.findall(r'data-peak-reference="([^"]+)"', html)
        )
        next_offset = _required_fixture(html, r'data-peak-offset="([^"]+)"', "offset")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=PeakDistrictCheckpointV1(row_offset=next_offset),
            complete=next_offset == "complete",
        )

    async def _discover_live(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        expected_end = self._today()
        expected_start = expected_end - timedelta(days=6)
        if window.start != expected_start or window.end != expected_end:
            raise PeakDistrictWindowUnsupportedError(expected_start, expected_end)
        progress = checkpoint or PeakDistrictCheckpointV1(row_offset="live")
        _assert_window(progress, window)
        if progress.live_complete:
            if window.include_open:
                raise PeakDistrictOpenEnumerationUnsupportedError
            yield DiscoveryBatch(references=(), next_checkpoint=progress, complete=True)
            return
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(_WEEKLY_URL), intent=RequestIntent.SEARCH)
        )
        parsed = _parse_weekly_results(capture.body, window)
        seen = set(progress.seen_references)
        fresh = []
        for item in parsed:
            if item.reference not in seen:
                seen.add(item.reference)
                fresh.append(item)
        completed = progress.model_copy(
            update={
                "window_start": window.start,
                "window_end": window.end,
                "seen_references": tuple(seen),
                "live_complete": not window.include_open,
            }
        )
        yield DiscoveryBatch(
            references=tuple(fresh),
            next_checkpoint=completed,
            complete=completed.live_complete,
        )
        if window.include_open:
            raise PeakDistrictOpenEnumerationUnsupportedError

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        """Read fixture summary or the captured legacy result page."""
        if session.mode == TransportMode.FIXTURE:
            return await self._fetch_fixture(session, reference)
        return await self._fetch_live(session, reference)

    async def _fetch_fixture(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        encoded = quote(reference.reference, safe="")
        detail = await session.fetch(
            PortalRequest(
                url=HttpUrl(f"{ASSURE_BASE}/Planning/Details/{encoded}"),
                intent=RequestIntent.DETAIL,
            )
        )
        html = detail.body.decode()
        payload = PeakDistrictApplicationV1(
            park_reference=reference.reference,
            record_type=_required_fixture(html, r'data-peak-type="([^"]+)"', "type"),
            proposal_summary=unescape(
                _required_fixture(html, r'data-peak-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required_fixture(
                html, r'data-peak-status="([^"]+)"', "status"
            ),
            parish=_required_fixture(html, r'data-peak-parish="([^"]+)"', "parish"),
            legacy_record_url=HttpUrl(
                _required_fixture(html, r'data-peak-legacy="([^"]+)"', "legacy URL")
            ),
        )
        return _snapshot(reference, payload, detail)

    async def _fetch_live(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        if reference.source_id != LEGACY_SOURCE or reference.locator is None:
            raise PeakDistrictRoutingError(reference.reference)
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(reference.locator), intent=RequestIntent.DETAIL)
        )
        fields, loading_sections, assure_url = _parse_detail(detail.body)
        published = _required_field(fields, "reference", "application reference")
        if published != reference.reference:
            raise PeakDistrictReferenceMismatchError(reference.reference, published)
        payload = PeakDistrictApplicationV1(
            park_reference=published,
            record_type=_required_field(
                fields, "application type", "record type", "type"
            ),
            proposal_summary=_required_field(fields, "description", "proposal"),
            case_status=_required_field(fields, "status"),
            parish=_required_field(fields, "parish"),
            legacy_record_url=HttpUrl(reference.locator),
            development_address=_optional_field(
                fields, "development address", "address"
            ),
            planning_portal_reference=_optional_field(
                fields, "planning portal reference"
            ),
            validated_date=_optional_date(fields, "validated date", "date validated"),
            assurelive_url=assure_url,
            loading_sections=loading_sections,
        )
        return _snapshot(reference, payload, detail)

    def normalise(
        self,
        snapshot: NativeSnapshot[PeakDistrictApplicationV1],
    ) -> NormalisedObservation:
        """Map Peak District visible summary values to the common record."""
        payload = snapshot.payload
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=payload.proposal_summary,
            status=payload.case_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="peak-district-v2",
            metadata=ApplicationMetadata(
                application_type=payload.record_type,
                address=payload.development_address,
                validated_date=payload.validated_date,
                aliases=(
                    ()
                    if payload.planning_portal_reference is None
                    else (payload.planning_portal_reference,)
                ),
                source_url=snapshot.evidence[0].url,
            ),
        )


def _snapshot(
    reference: SourceReference,
    payload: PeakDistrictApplicationV1,
    detail: EvidenceCapture,
) -> NativeSnapshot[PeakDistrictApplicationV1]:
    loading = bool(payload.loading_sections)
    return NativeSnapshot(
        reference=reference,
        observed_at=datetime.now(UTC),
        payload=payload,
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=(
                FailedSection(code="client-section-loading")
                if loading
                else UnavailableSection(
                    reason="AssureLive document collection is unresolved"
                )
            ),
            comments=(
                FailedSection(code="client-section-loading")
                if loading
                else UnavailableSection(
                    reason="legacy comment enumeration is unresolved"
                )
            ),
        ),
        evidence=(detail,),
    )


def _parse_weekly_results(
    body: bytes, window: DiscoveryWindow
) -> tuple[SourceReference, ...]:
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("#searchresults")
    if not isinstance(table, Tag):
        _raise_parse("#searchresults")
    references = []
    for row in table.select("tbody tr") or table.select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        link = row.select_one('a[href*="/result/"]')
        if not isinstance(link, Tag):
            _raise_parse("opaque result link")
        row_date = _row_date(cells)
        if not window.start <= row_date <= window.end:
            raise PeakDistrictResultWindowError(row_date)
        reference = cells[0].get_text(" ", strip=True)
        if not reference:
            _raise_parse("weekly reference")
        references.append(
            SourceReference(
                source_id=LEGACY_SOURCE,
                reference=reference,
                locator=urljoin(f"{LEGACY_BASE}/", str(link.get("href", ""))),
            )
        )
    reported = _reported_count(soup)
    if reported != len(references):
        raise PeakDistrictCountMismatchError(reported, len(references))
    return tuple(references)


def _reported_count(soup: BeautifulSoup) -> int:
    element = soup.select_one("[data-result-count]")
    if isinstance(element, Tag):
        return int(str(element.get("data-result-count")))
    text = soup.get_text(" ", strip=True)
    if "no entries" in text.casefold() or "no results" in text.casefold():
        return 0
    match = re.search(
        r"(?:showing.*?of|total)\s+(\d+)\s+(?:entries|results)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        _raise_parse("reported result count")
    return int(match.group(1))


def _row_date(cells: list[Tag]) -> date:
    for cell in cells:
        parsed = _parse_date(cell.get_text(" ", strip=True))
        if parsed is not None:
            return parsed
    return _raise_parse("weekly row date")


def _parse_detail(
    body: bytes,
) -> tuple[dict[str, str], tuple[str, ...], HttpUrl | None]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for row in soup.select(".dataview tr, table.details tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= _MINIMUM_LABELLED_CELLS:
            fields[_normalise_label(cells[0].get_text(" ", strip=True))] = cells[
                -1
            ].get_text(" ", strip=True)
    for term in soup.select(".dataview dt, dl.details dt"):
        value = term.find_next_sibling("dd")
        if isinstance(value, Tag):
            fields[_normalise_label(term.get_text(" ", strip=True))] = value.get_text(
                " ", strip=True
            )
    if not fields:
        _raise_parse("legacy labelled detail")
    loading = tuple(
        str(item.get("id") or item.get("data-section") or "unknown")
        for item in soup.select("[id], [data-section]")
        if item.get_text(" ", strip=True).casefold() == "loading..."
    )
    assure = soup.select_one(f'a[href^="{ASSURE_BASE}"]')
    assure_url = (
        None if not isinstance(assure, Tag) else HttpUrl(str(assure.get("href", "")))
    )
    return fields, loading, assure_url


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
    parsed = _parse_date(value)
    if parsed is None:
        _raise_parse(f"date {'/'.join(names)}")
    return parsed


def _parse_date(value: str) -> date | None:
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


def _assert_window(
    checkpoint: PeakDistrictCheckpointV1, window: DiscoveryWindow
) -> None:
    if checkpoint.window_start is not None and (
        checkpoint.window_start != window.start or checkpoint.window_end != window.end
    ):
        raise PeakDistrictCheckpointError


def _required_fixture(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        _raise_parse(field)
    return match.group(1).strip()


class PeakDistrictParseError(ValueError):
    """A required Peak District boundary value was absent."""

    def __init__(self, field: str) -> None:
        """Name a safe parser field."""
        self.code = f"parse-{re.sub(r'[^a-z0-9]+', '-', field.casefold()).strip('-')}"
        super().__init__(f"missing Peak District field {field}")


class PeakDistrictWindowUnsupportedError(ValueError):
    """The requested dates cannot be proven by the weekly route."""

    def __init__(self, start: date, end: date) -> None:
        """Report the only currently supported live interval."""
        super().__init__(f"Peak District live discovery requires {start} through {end}")


class PeakDistrictCountMismatchError(ValueError):
    """The DataTable count did not match all DOM rows."""

    def __init__(self, expected: int, actual: int) -> None:
        """Report only counts."""
        super().__init__(
            f"Peak District reported {expected} results but exposed {actual}"
        )


class PeakDistrictResultWindowError(ValueError):
    """The rolling route returned a row outside the requested week."""

    def __init__(self, observed: date) -> None:
        """Report the unexpected public date."""
        super().__init__(f"Peak District returned out-of-window row dated {observed}")


class PeakDistrictCheckpointError(ValueError):
    """A saved cursor belongs to another rolling week."""


class PeakDistrictOpenEnumerationUnsupportedError(RuntimeError):
    """Older open applications cannot yet be enumerated completely."""

    def __init__(self) -> None:
        """Keep the unsupported boundary explicit."""
        super().__init__("Peak District older-open enumeration is not verified")


class PeakDistrictRoutingError(ValueError):
    """A reference lacks the opaque legacy result locator."""

    def __init__(self, reference: str) -> None:
        """Identify the human reference only."""
        super().__init__(f"Peak District cannot route reference {reference}")


class PeakDistrictReferenceMismatchError(ValueError):
    """A detail response published a different reference."""

    def __init__(self, expected: str, actual: str) -> None:
        """Report the conflicting public references."""
        super().__init__(
            f"expected Peak District reference {expected}, received {actual}"
        )


def _raise_parse(field: str) -> NoReturn:
    raise PeakDistrictParseError(field)
