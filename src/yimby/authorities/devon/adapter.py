# Copyright (c) 2026 Kostas Stathoulopoulos

"""Devon County Council register adapter."""

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
    DocumentRecord,
    ExcludedSection,
    FrozenModel,
    NativeDocument,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    collection_state,
)
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("devon-custom-register")
BASE_URL = "https://planning.devon.gov.uk"


class DevonCheckpointV1(FrozenModel):
    """Devon received-date result cursor."""

    result_page: str


class DevonApplicationV1(FrozenModel):
    """Devon-native minerals, waste, or county development record."""

    council_reference: str
    application_type: str
    proposal_description: str
    public_status: str
    site_location: str
    documents: tuple[NativeDocument, ...]


class DevonParseError(ValueError):
    """A required Devon field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Devon field {field}")


class DevonAdapter:
    """Keep Devon's disclaimer and county record semantics local."""

    manifest = AuthorityManifest(
        id=AuthorityId("devon"),
        name="Devon County Council",
        kind=AuthorityKind.COUNTY,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: DevonCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[DevonCheckpointV1]]:
        """Read a sanitised accepted-disclaimer search result."""
        page = "1" if checkpoint is None else checkpoint.result_page
        url = (
            f"{BASE_URL}/Search/Results?receivedFrom={window.start.isoformat()}"
            f"&receivedTo={window.end.isoformat()}&page={quote(page)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-devon-reference="([^"]+)"', html)
        )
        next_page = _required(html, r'data-devon-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=DevonCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[DevonApplicationV1]:
        """Read Devon detail and hidden-tab document metadata."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/Application/Detail?ref={encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        documents = tuple(
            NativeDocument(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-devon-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = DevonApplicationV1(
            council_reference=reference.reference,
            application_type=_required(
                html,
                r'data-devon-type="([^"]+)"',
                "application type",
            ),
            proposal_description=unescape(
                _required(html, r'data-devon-proposal="([^"]+)"', "proposal")
            ),
            public_status=_required(
                html,
                r'data-devon-status="([^"]+)"',
                "status",
            ),
            site_location=unescape(
                _required(html, r'data-devon-location="([^"]+)"', "location")
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
                comments=ExcludedSection(
                    policy="public responses retained as document metadata only"
                ),
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[DevonApplicationV1],
    ) -> NormalisedObservation:
        """Map Devon county fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_description,
            status=snapshot.payload.public_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in snapshot.payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="devon-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise DevonParseError(field)
    return match.group(1).strip()
