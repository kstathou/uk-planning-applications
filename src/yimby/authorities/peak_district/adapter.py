# Copyright (c) 2026 Kostas Stathoulopoulos

"""Peak District legacy and AssureLive adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import quote

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
    UnavailableSection,
)
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

LEGACY_SOURCE = SourceId("peak-district-legacy")
LEGACY_BASE = "https://portal.peakdistrict.gov.uk"
ASSURE_BASE = "https://planning.peakdistrict.gov.uk/AssureLive"


class PeakDistrictCheckpointV1(FrozenModel):
    """Peak District client-side result cursor."""

    row_offset: str


class PeakDistrictApplicationV1(FrozenModel):
    """Peak District-native record across legacy and replacement portals."""

    park_reference: str
    record_type: str
    proposal_summary: str
    case_status: str
    parish: str
    legacy_record_url: HttpUrl


class PeakDistrictParseError(ValueError):
    """A required Peak District field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Peak District field {field}")


class PeakDistrictAdapter:
    """Keep portal migration and loading-state rules authority-local."""

    manifest = AuthorityManifest(
        id=AuthorityId("peak-district"),
        name="Peak District National Park Authority",
        kind=AuthorityKind.NATIONAL_PARK,
        sources=(
            SourceDefinition(
                id=LEGACY_SOURCE,
                base_url=HttpUrl(f"{LEGACY_BASE}/"),
                valid_to=date(2026, 9, 15),
            ),
            SourceDefinition(
                id=SourceId("peak-district-assurelive"),
                base_url=HttpUrl(f"{ASSURE_BASE}/"),
                valid_from=date(2025, 1, 1),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: PeakDistrictCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[PeakDistrictCheckpointV1]]:
        """Read all sanitised rows rather than the visible first page."""
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
        next_offset = _required(html, r'data-peak-offset="([^"]+)"', "offset")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=PeakDistrictCheckpointV1(row_offset=next_offset),
            complete=next_offset == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[PeakDistrictApplicationV1]:
        """Read a sanitised AssureLive summary with unknown sections explicit."""
        encoded = quote(reference.reference, safe="")
        url = f"{ASSURE_BASE}/Planning/Details/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = PeakDistrictApplicationV1(
            park_reference=reference.reference,
            record_type=_required(html, r'data-peak-type="([^"]+)"', "type"),
            proposal_summary=unescape(
                _required(html, r'data-peak-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required(
                html,
                r'data-peak-status="([^"]+)"',
                "status",
            ),
            parish=_required(html, r'data-peak-parish="([^"]+)"', "parish"),
            legacy_record_url=HttpUrl(
                _required(html, r'data-peak-legacy="([^"]+)"', "legacy URL")
            ),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=UnavailableSection(
                    reason="replacement document section not verified"
                ),
                comments=UnavailableSection(
                    reason="legacy comment section not enumerated"
                ),
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[PeakDistrictApplicationV1],
    ) -> NormalisedObservation:
        """Map Peak District summary values to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_summary,
            status=snapshot.payload.case_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="peak-district-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise PeakDistrictParseError(field)
    return match.group(1).strip()
