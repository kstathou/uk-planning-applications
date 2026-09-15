# Copyright (c) 2026 Kostas Stathoulopoulos

"""Blackburn with Darwen Planning Explorer adapter."""

from __future__ import annotations

import re
from datetime import UTC, datetime
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

SOURCE = SourceId("blackburn-northgate-explorer")
BASE_URL = "https://planning.blackburn.gov.uk/Northgate/PlanningExplorer"


class BlackburnWithDarwenCheckpointV1(FrozenModel):
    """Blackburn Planning Explorer page cursor."""

    result_page: str


class BlackburnWithDarwenApplicationV1(FrozenModel):
    """Blackburn-native Planning Explorer record."""

    explorer_key: str
    council_reference: str
    development_proposal: str
    public_status: str
    planning_area: str


class BlackburnWithDarwenParseError(ValueError):
    """A required Blackburn with Darwen field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Blackburn with Darwen field {field}")


class BlackburnWithDarwenAdapter:
    """Keep Blackburn's Northgate record shape independent."""

    manifest = AuthorityManifest(
        id=AuthorityId("blackburn-with-darwen"),
        name="Blackburn with Darwen Borough Council",
        kind=AuthorityKind.UNITARY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BlackburnWithDarwenCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BlackburnWithDarwenCheckpointV1]]:
        """Read a fixture result without treating maintenance as empty."""
        page = "1" if checkpoint is None else checkpoint.result_page
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
            for value in re.findall(r'data-blackburn-reference="([^"]+)"', html)
        )
        next_page = _required(
            html,
            r'data-blackburn-page="([^"]+)"',
            "page",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=BlackburnWithDarwenCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BlackburnWithDarwenApplicationV1]:
        """Read a fixture record without claiming maintenance-page recovery."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = BlackburnWithDarwenApplicationV1(
            explorer_key=_required(
                html,
                r'data-blackburn-key="([^"]+)"',
                "explorer key",
            ),
            council_reference=reference.reference,
            development_proposal=unescape(
                _required(html, r'data-blackburn-proposal="([^"]+)"', "proposal")
            ),
            public_status=_required(
                html,
                r'data-blackburn-status="([^"]+)"',
                "status",
            ),
            planning_area=_required(
                html,
                r'data-blackburn-area="([^"]+)"',
                "planning area",
            ),
        )
        unavailable = UnavailableSection(reason="live portal was under maintenance")
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
        snapshot: NativeSnapshot[BlackburnWithDarwenApplicationV1],
    ) -> NormalisedObservation:
        """Map Blackburn fixture fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.development_proposal,
            status=snapshot.payload.public_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="blackburn-with-darwen-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise BlackburnWithDarwenParseError(field)
    return match.group(1).strip()
