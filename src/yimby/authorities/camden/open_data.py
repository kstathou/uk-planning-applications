# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden's official Socrata planning feed; no planning-portal requests."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import urlencode

import httpx
from pydantic import ConfigDict, Field, HttpUrl, TypeAdapter

from yimby.authorities.camden.adapter import CamdenAdapter
from yimby.authorities.camden.discovery import CAMDEN_SOURCE
from yimby.domain import (
    ApplicationMetadata,
    AuthorityCapabilities,
    CapabilityState,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryEvidenceCapture,
    DiscoveryWindow,
    EvidenceCapture,
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
    UnavailableSection,
)
from yimby.geo import bng_to_wgs84
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

DATASET_URL = "https://opendata.camden.gov.uk/resource/2eiu-s2cw.json"
PAGE_SIZE = 1000
_ROWS = TypeAdapter(list[dict[str, object]])


class CamdenOpenDataError(ValueError):
    """The feed cannot prove a complete, unambiguous application result."""


def _fail(reason: str) -> NoReturn:
    raise CamdenOpenDataError(reason)


class CamdenOpenDataApplicationV1(FrozenModel):
    """Published application fields, preserving additional source columns."""

    model_config = ConfigDict(frozen=True, extra="allow")
    pk: str = Field(pattern=r"^\d+$")
    application_number: str = Field(min_length=1)
    development_description: str = ""
    development_address: str | None = None
    system_status: str | None = None
    decision_type: str | None = None
    application_type: str | None = None
    registered_date: datetime | None = None
    valid_from_date: datetime | None = None
    decision_date: datetime | None = None
    full_application: dict[str, str] | None = None
    easting: float | None = None
    northing: float | None = None
    case_officer: str | None = None


class CamdenOpenDataCheckpointV1(FrozenModel):
    """Scope-bound keyset cursor with source total and upload watermark."""

    kind: Literal["camden-open-data"] = "camden-open-data"
    window: DiscoveryWindow
    last_pk: int = Field(default=-1, ge=-1)
    enumerated: int = Field(default=0, ge=0)
    expected: int = Field(ge=0)
    watermark: str
    complete: bool = False


def api_url(**parameters: str) -> HttpUrl:
    """Encode SoQL values; credentials never appear in URLs or evidence."""
    return HttpUrl(f"{DATASET_URL}?{urlencode(parameters)}")


def _discovery_capture(
    capture: EvidenceCapture,
    request: PortalRequest,
) -> DiscoveryEvidenceCapture:
    """Bind a retained API response to the exact public request that produced it."""
    return DiscoveryEvidenceCapture(
        capture=capture,
        request_url=request.url,
        request_method=request.method.value,
        request_form=tuple((field.name, field.value) for field in request.form),
    )


def scope_filter(window: DiscoveryWindow) -> str:
    """Include dated changes and all source-designated open/unknown cases."""
    if window.end < window.start:
        _fail("end precedes start")
    start = window.start.isoformat()
    end = (window.end + timedelta(days=1)).isoformat()
    clauses = [
        f"({field} >= '{start}T00:00:00' AND {field} < '{end}T00:00:00')"
        for field in (
            "registered_date",
            "valid_from_date",
            "decision_date",
            "system_status_change_date",
        )
    ]
    if window.include_open:
        clauses.append(
            "(system_status in ('Registered','Appeal Lodged') OR system_status IS NULL)"
        )
    return "(" + " OR ".join(clauses) + ")"


def create_session(limiter: HostRateLimiter | None = None) -> HttpxPortalSession:
    """Use the public SODA endpoint with an optional header-only app token."""
    headers = {
        "accept": "application/json",
        "user-agent": "yimby/0.1 Camden open-data client",
    }
    token = os.environ.get("CAMDEN_SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    return HttpxPortalSession(
        client=httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=False),
        limiter=limiter,
    )


def _canonical(row: dict[str, object]) -> CamdenOpenDataApplicationV1:
    # Upload time and duplicate row ID are ingestion metadata, not source changes.
    return CamdenOpenDataApplicationV1.model_validate(
        {
            key: value
            for key, value in row.items()
            if key not in {"last_uploaded", "socrata_id"}
        }
    )


class CamdenOpenDataAdapter:
    """Bulk-load API pages and reuse them for per-application persistence."""

    manifest = CamdenAdapter.manifest.model_copy(
        update={
            "live_status": LiveStatus(
                readiness=LiveReadiness.LIVE_READY,
                transport=LiveTransportKind.HTTP,
                reason=(
                    "Official Socrata application metadata feed; documents and "
                    "comment text unsupported; weekly qualification pending"
                ),
                evidence=(
                    (
                        "2026-09-16: 1,499 applications, four requests per pass, "
                        "unchanged immediate refresh and verified evidence"
                    ),
                ),
            ),
            "sources": (
                *CamdenAdapter.manifest.sources,
                SourceDefinition(
                    id=SourceId("camden-socrata-2eiu-s2cw"),
                    base_url=HttpUrl(DATASET_URL),
                    valid_from=date(2010, 1, 1),
                ),
            ),
            "capabilities": AuthorityCapabilities(
                documents=CapabilityState.UNSUPPORTED,
                comments=CapabilityState.UNSUPPORTED,
                coordinates=CapabilityState.SUPPORTED,
            ),
        }
    )

    def __init__(self) -> None:
        """Keep only the active page's parsed records in memory."""
        self._cache: dict[str, tuple[CamdenOpenDataApplicationV1, EvidenceCapture]] = {}
        self._cache_session: PortalSession | None = None

    async def _summary(
        self, session: PortalSession, where: str
    ) -> tuple[int, str, DiscoveryEvidenceCapture]:
        request = PortalRequest(
            url=api_url(
                **{
                    "$select": (
                        "count(distinct pk) AS total,"
                        "count(distinct application_number) AS references,"
                        "count(distinct (pk || '|' || application_number)) "
                        "AS pairs,"
                        "max(last_uploaded) AS watermark"
                    ),
                    "$where": where,
                }
            ),
            intent=RequestIntent.SEARCH,
        )
        capture = await session.fetch(request)
        rows = _ROWS.validate_json(capture.body)
        if len(rows) != 1 or "total" not in rows[0]:
            _fail("missing source count")
        if not (rows[0].get("references") == rows[0].get("pairs") == rows[0]["total"]):
            _fail("application references and primary keys are not one-to-one")
        return (
            int(str(rows[0]["total"])),
            str(rows[0].get("watermark", "empty")),
            _discovery_capture(capture, request),
        )

    async def discover(  # noqa: C901 - One scope-bound pagination state machine.
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CamdenOpenDataCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CamdenOpenDataCheckpointV1]]:
        """Keyset-page complete PK groups, retaining every count and row response."""
        self._cache.clear()
        self._cache_session = session
        where = scope_filter(window)
        total, watermark, summary = await self._summary(session, where)
        if checkpoint is not None and not checkpoint.complete:
            if checkpoint.window != window or (
                checkpoint.expected,
                checkpoint.watermark,
            ) != (total, watermark):
                _fail("unfinished scope or upload changed; use a fresh data directory")
            cursor = checkpoint
        else:
            cursor = CamdenOpenDataCheckpointV1(
                window=window, expected=total, watermark=watermark
            )
        while True:
            previous_last_pk = cursor.last_pk
            request = PortalRequest(
                url=api_url(
                    **{
                        "$where": f"{where} AND pk > {cursor.last_pk}",
                        "$order": "pk ASC,socrata_id ASC",
                        "$limit": str(PAGE_SIZE),
                    }
                ),
                intent=RequestIntent.SEARCH,
            )
            capture = await session.fetch(request)
            rows = _ROWS.validate_json(capture.body)
            parsed = [_canonical(row) for row in rows]
            keys = [int(row.pk) for row in parsed]
            if keys != sorted(keys) or any(key <= cursor.last_pk for key in keys):
                _fail("invalid keyset order")
            terminal = len(rows) < PAGE_SIZE
            # Defer the final PK on a full page so all its duplicates are compared.
            if not terminal:
                parsed = [row for row in parsed if row.pk != parsed[-1].pk]
                if not parsed:
                    _fail("one application exceeds page capacity")
            unique: dict[str, CamdenOpenDataApplicationV1] = {}
            for row in parsed:
                previous = unique.get(row.application_number)
                if previous is not None and previous != row:
                    _fail("conflicting duplicate application rows")
                unique[row.application_number] = row
            self._cache = {ref: (row, capture) for ref, row in unique.items()}
            count = cursor.enumerated + len(unique)
            evidence: tuple[DiscoveryEvidenceCapture, ...] = (
                summary,
                _discovery_capture(capture, request),
            )
            if terminal:
                final_total, final_watermark, final_summary = await self._summary(
                    session, where
                )
                evidence = (*evidence, final_summary)
                if (final_total, final_watermark, count) != (total, watermark, total):
                    _fail("feed changed or enumeration count disagrees")
            cursor = CamdenOpenDataCheckpointV1(
                window=window,
                expected=total,
                watermark=watermark,
                last_pk=int(parsed[-1].pk) if parsed else cursor.last_pk,
                enumerated=count,
                complete=terminal,
            )
            yield DiscoveryBatch(
                references=tuple(
                    SourceReference(
                        source_id=CAMDEN_SOURCE, reference=ref, locator=row.pk
                    )
                    for ref, row in unique.items()
                ),
                next_checkpoint=cursor,
                complete=terminal,
                evidence=evidence,
                evidence_key=f"socrata:after:{previous_last_pk}",
                evidence_page=1,
            )
            if terminal:
                return

    async def fetch(
        self, session: PortalSession, reference: SourceReference
    ) -> NativeSnapshot[CamdenOpenDataApplicationV1]:
        """Serve a discovered row or refresh one reference via the same API."""
        if reference.source_id != CAMDEN_SOURCE:
            _fail("unexpected source identity")
        cached = (
            self._cache.pop(reference.reference, None)
            if self._cache_session is session
            else None
        )
        if cached is None:
            escaped = reference.reference.replace("'", "''")
            capture = await session.fetch(
                PortalRequest(
                    url=api_url(
                        **{
                            "$where": f"application_number = '{escaped}'",
                            "$limit": str(PAGE_SIZE),
                        }
                    ),
                    intent=RequestIntent.DETAIL,
                )
            )
            rows = _ROWS.validate_json(capture.body)
            if not rows or len(rows) >= PAGE_SIZE:
                _fail("missing or excessive exact-reference rows")
            payload = _canonical(rows[0])
            if any(_canonical(row) != payload for row in rows[1:]):
                _fail("conflicting exact-reference rows")
        else:
            payload, capture = cached
        if (
            payload.application_number != reference.reference
            or reference.locator not in {None, payload.pk}
        ):
            _fail("application identity changed")
        return NativeSnapshot(
            reference=reference.model_copy(update={"locator": payload.pk}),
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=UnavailableSection(
                    reason="Document index not published in Camden dataset 2eiu-s2cw"
                ),
                comments=UnavailableSection(
                    reason="Comment text not published in Camden dataset 2eiu-s2cw"
                ),
            ),
            evidence=(capture,),
        )

    def normalise(
        self, snapshot: NativeSnapshot[CamdenOpenDataApplicationV1]
    ) -> NormalisedObservation:
        """Map published API fields while retaining raw source evidence."""
        row = snapshot.payload
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=row.development_description,
            status=(row.system_status or "unknown").casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=snapshot.evidence[0].digest),
                Provenance(field="status", evidence=snapshot.evidence[0].digest),
            ),
            normaliser_version="camden-open-data-v1",
            metadata=ApplicationMetadata(
                address=row.development_address,
                application_type=row.application_type,
                decision=row.decision_type,
                validated_date=row.valid_from_date.date()
                if row.valid_from_date
                else None,
                decision_date=row.decision_date.date() if row.decision_date else None,
                location=bng_to_wgs84(row.easting, row.northing),
                source_url=HttpUrl(row.full_application["url"])
                if row.full_application and row.full_application.get("url")
                else snapshot.evidence[0].url,
                officer_name=row.case_officer,
            ),
        )
