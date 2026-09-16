# Copyright (c) 2026 Kostas Stathoulopoulos

"""OPDC Agile Applications adapter."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import quote

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
    UnavailableSection,
)
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("opdc-agile-applications")
BASE_URL = "https://planning.agileapplications.co.uk/opdc"


class OpdcCheckpointV1(FrozenModel):
    """OPDC Agile search page token."""

    page_token: str


class OpdcApplicationV1(FrozenModel):
    """OPDC-native delegated planning application."""

    agile_case_id: str
    development_description: str
    workflow_status: str
    site_name: str


class OpdcParseError(ValueError):
    """A required OPDC field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing OPDC field {field}")


class OpdcAdapter:
    """Keep OPDC's Agile identifiers and inconclusive sections local."""

    manifest = AuthorityManifest(
        id=AuthorityId("opdc"),
        name="Old Oak and Park Royal Development Corporation",
        kind=AuthorityKind.DEVELOPMENT_CORPORATION,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(BASE_URL)),),
        capabilities=AuthorityCapabilities(discovery=CapabilityState.UNKNOWN),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: OpdcCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[OpdcCheckpointV1]]:
        """Read a sanitised Agile bootstrap result."""
        page = "first" if checkpoint is None else checkpoint.page_token
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
            for value in re.findall(r'data-opdc-reference="([^"]+)"', html)
        )
        next_page = _required(html, r'data-opdc-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=OpdcCheckpointV1(page_token=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[OpdcApplicationV1]:
        """Read a fixture record without claiming live Agile agreement."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = OpdcApplicationV1(
            agile_case_id=_required(
                html,
                r'data-opdc-case-id="([^"]+)"',
                "case id",
            ),
            development_description=unescape(
                _required(html, r'data-opdc-proposal="([^"]+)"', "proposal")
            ),
            workflow_status=_required(
                html,
                r'data-opdc-status="([^"]+)"',
                "status",
            ),
            site_name=unescape(_required(html, r'data-opdc-site="([^"]+)"', "site")),
        )
        unavailable = UnavailableSection(reason="live client bootstrap inconclusive")
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
        snapshot: NativeSnapshot[OpdcApplicationV1],
    ) -> NormalisedObservation:
        """Map OPDC fixture fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.development_description,
            status=snapshot.payload.workflow_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="opdc-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise OpdcParseError(field)
    return match.group(1).strip()
