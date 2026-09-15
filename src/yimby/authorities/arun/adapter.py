# Copyright (c) 2026 Kostas Stathoulopoulos

"""Arun Ocella register adapter."""

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
    FrozenModel,
    NativeDocument,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    UnavailableSection,
    collection_state,
)
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("arun-ocella")
BASE_URL = "https://www1.arun.gov.uk/aplanning/OcellaWeb"


class ArunCheckpointV1(FrozenModel):
    """Arun Ocella result-row cursor."""

    result_row: str


class ArunApplicationV1(FrozenModel):
    """Arun-native Ocella application."""

    ocella_reference: str
    proposal_text: str
    decision_status: str
    parish_name: str
    documents: tuple[NativeDocument, ...]


class ArunParseError(ValueError):
    """A required Arun field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Arun field {field}")


class ArunAdapter:
    """Keep Ocella actions and native document types authority-local."""

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
        """Read an Ocella search result capture."""
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
        next_row = _required(html, r'data-arun-row="([^"]+)"', "result row")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=ArunCheckpointV1(result_row=next_row),
            complete=next_row == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[ArunApplicationV1]:
        """Read Ocella detail and document index metadata."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/PlanningDetails?reference={encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
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
                _required(html, r'data-arun-proposal="([^"]+)"', "proposal")
            ),
            decision_status=_required(
                html,
                r'data-arun-status="([^"]+)"',
                "status",
            ),
            parish_name=_required(
                html,
                r'data-arun-parish="([^"]+)"',
                "parish",
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

    def normalise(
        self,
        snapshot: NativeSnapshot[ArunApplicationV1],
    ) -> NormalisedObservation:
        """Map Arun Ocella fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_text,
            status=snapshot.payload.decision_status.casefold().replace(" ", "-"),
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
            normaliser_version="arun-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise ArunParseError(field)
    return match.group(1).strip()
