# Copyright (c) 2026 Kostas Stathoulopoulos

"""Cheshire East-owned fixture and valid-date search adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote, urljoin

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
        async for batch in self._discover_live(session, window, checkpoint):
            yield batch

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

    async def _discover_live(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CheshireEastCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CheshireEastCheckpointV1]]:
        progress = checkpoint or CheshireEastCheckpointV1(search_page="live")
        _assert_window(progress, window)
        if progress.table_observed:
            raise CheshireEastResultCompletenessUnavailableError
        form_capture = await session.fetch(
            PortalRequest(url=HttpUrl(_SEARCH_URL), intent=RequestIntent.SEARCH)
        )
        form = _parse_search_form(form_capture.body)
        result_capture = await session.fetch(_valid_date_request(form, window))
        results = _parse_result_table(result_capture.body)
        seen = set(progress.seen_references)
        references = []
        for result in results:
            if result.public_reference not in seen:
                seen.add(result.public_reference)
                references.append(
                    SourceReference(
                        source_id=SOURCE,
                        reference=result.public_reference,
                        locator=str(result.detail_locator),
                    )
                )
        observed = progress.model_copy(
            update={
                "window_start": window.start,
                "window_end": window.end,
                "seen_references": tuple(seen),
                "table_observed": True,
            }
        )
        yield DiscoveryBatch(
            references=tuple(references), next_checkpoint=observed, complete=False
        )
        raise CheshireEastResultCompletenessUnavailableError

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
    _named_control(form, "valid_date_from")
    return form


def _named_control(form: Tag, name: str) -> Tag:
    control = form.find(None, {"name": name})
    if not isinstance(control, Tag):
        _raise_parse(name)
    return control


def _valid_date_request(form: Tag, window: DiscoveryWindow) -> PortalRequest:
    start = _named_control(form, "valid_date_from")
    values = {str(start.get("name")): window.start.strftime("%d/%m/%Y")}
    fields = []
    for item in form.select("input[name], select[name], textarea[name]"):
        name = str(item["name"])
        input_type = str(item.get("type", "text")).casefold()
        if input_type in {"button", "image", "reset", "submit"}:
            continue
        value = values.get(name, str(item.get("value", "")))
        fields.append(FormField(name=name, value=value))
    return PortalRequest(
        url=HttpUrl(urljoin(f"{BASE_URL}/", str(form.get("action", "index.html")))),
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=tuple(fields),
    )


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


class CheshireEastRoutingError(ValueError):
    """A reference lacks the verified View locator."""

    def __init__(self, reference: str) -> None:
        """Identify the public reference only."""
        super().__init__(f"Cheshire East cannot route reference {reference}")


class CheshireEastCheckpointError(ValueError):
    """A saved table cursor belongs to another date window."""


def _raise_parse(field: str) -> NoReturn:
    raise CheshireEastParseError(field)
