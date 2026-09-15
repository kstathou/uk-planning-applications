# Copyright (c) 2026 Kostas Stathoulopoulos

"""Dorset Explorer planning adapter."""

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

SOURCE = SourceId("dorset-explorer-esri")
BASE_URL = "https://gi.dorsetcouncil.gov.uk/dorsetexplorer/planning/public"


class DorsetCheckpointV1(FrozenModel):
    """Dorset Explorer result offset."""

    object_offset: str


class DorsetApplicationV1(FrozenModel):
    """Dorset-native Esri planning feature."""

    esri_object_id: int
    application_reference: str
    proposal_description: str
    decision_status: str
    ward_name: str


class DorsetParseError(ValueError):
    """A required Dorset feature field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Dorset field {field}")


class DorsetAdapter:
    """Keep Dorset's Esri feature vocabulary local."""

    manifest = AuthorityManifest(
        id=AuthorityId("dorset"),
        name="Dorset Council",
        kind=AuthorityKind.UNITARY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(BASE_URL)),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DorsetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DorsetCheckpointV1]]:
        """Read a sanitised Esri query response capture."""
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

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DorsetApplicationV1]:
        """Read a rendered fixture without claiming live Esri agreement."""
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


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise DorsetParseError(field)
    return match.group(1).strip()
