# Copyright (c) 2026 Kostas Stathoulopoulos

"""Arun-owned fixture and live Ocella planning-register adapter."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, Self, cast
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import Field, HttpUrl, model_validator

from yimby.domain import (
    ApplicationEvent,
    ApplicationMetadata,
    ApplicationRelationship,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryEvidenceCapture,
    DiscoveryWindow,
    DocumentRecord,
    EvidenceCapture,
    EvidenceDigest,
    FrozenModel,
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
_DOCUMENT_COLUMNS = 5
_RESULT_COLUMNS = 4
_OPEN_HISTORY_START = date(1948, 1, 1)
_OPEN_ANNUAL_END = date(2023, 12, 31)
_DECEMBER = 12
_RESULT_CAP = 200
_QUERY_FIELDS = (
    "reference",
    "location",
    "OcellaPlanningSearch.postcode",
    "area",
    "applicant",
    "agent",
    "undecided",
    "type",
    "receivedFrom",
    "receivedTo",
    "decidedFrom",
    "decidedTo",
)


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


type ArunQuery = Annotated[
    ArunReceivedQuery | ArunDecidedQuery | ArunOpenReceivedQuery,
    Field(discriminator="kind"),
]


class ArunRequestContract(FrozenModel):
    """Expected request contract for one query result capture."""

    url: HttpUrl
    method: RequestMethod
    form: tuple[FormField, ...]


def _canonical_query_plan(scope: ArunDiscoveryScope) -> tuple[ArunQuery, ...]:
    plan: list[ArunQuery] = [
        ArunReceivedQuery(start=scope.start, end=scope.end),
        ArunDecidedQuery(start=scope.start, end=scope.end),
    ]
    if not scope.include_open:
        return tuple(plan)
    if scope.end < _OPEN_HISTORY_START:
        return tuple(plan)
    first_partition_end = min(scope.end, date(1999, 12, 31))
    plan.append(
        ArunOpenReceivedQuery(
            start=_OPEN_HISTORY_START,
            end=first_partition_end,
        )
    )
    if scope.end <= first_partition_end:
        return tuple(plan)
    plan.extend(
        ArunOpenReceivedQuery(
            start=date(year, 1, 1),
            end=min(scope.end, date(year, 12, 31)),
        )
        for year in range(
            2000,
            min(scope.end.year, _OPEN_ANNUAL_END.year) + 1,
        )
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


class ArunCompletedQuery(FrozenModel):
    """One fully enumerated query in plan order."""

    key: str
    reported_count: int | None = Field(default=None, ge=0, lt=_RESULT_CAP)
    enumerated_count: int = Field(ge=0, lt=_RESULT_CAP)
    references: tuple[str, ...]
    initial_evidence: EvidenceDigest
    expanded_evidence: EvidenceDigest | None = None
    initial_request: ArunRequestContract | None = None
    expanded_request: ArunRequestContract | None = None

    @model_validator(mode="after")
    def counts_agree(self) -> Self:
        """Reject summaries that conceal incomplete enumeration."""
        if (
            (
                self.reported_count is not None
                and self.reported_count != self.enumerated_count
            )
            or self.enumerated_count != len(self.references)
            or len(self.references) != len(set(self.references))
        ):
            message = "Arun completed-query counts disagree"
            raise ValueError(message)
        return self


class ArunReady(FrozenModel):
    """Live discovery can start the next canonical query."""

    kind: Literal["ready"] = "ready"
    next_query: int = Field(ge=0)
    completed: tuple[ArunCompletedQuery, ...] = ()
    seen_references: tuple[str, ...] = ()


class ArunAwaitingShowAll(FrozenModel):
    """The active query must be replayed before its expansion."""

    kind: Literal["show-all"] = "show-all"
    next_query: int = Field(ge=0)
    completed: tuple[ArunCompletedQuery, ...] = ()
    reported_count: int = Field(gt=0, lt=_RESULT_CAP)
    initial_references: tuple[str, ...] = Field(min_length=1)
    initial_evidence: EvidenceDigest
    initial_request: ArunRequestContract | None = None
    seen_references: tuple[str, ...] = ()


class ArunComplete(FrozenModel):
    """Every query in the canonical live plan is complete."""

    kind: Literal["complete"] = "complete"
    completed: tuple[ArunCompletedQuery, ...]
    seen_references: tuple[str, ...] = ()


type ArunProgress = Annotated[
    ArunReady | ArunAwaitingShowAll | ArunComplete,
    Field(discriminator="kind"),
]


class ArunFixtureCursor(FrozenModel):
    """Cursor for deterministic fixture replay."""

    mode: Literal["fixture"] = "fixture"
    result_row: str


class ArunLegacyLiveCursor(FrozenModel):
    """Decoded pre-query-plan V1 state, restarted at the current boundary."""

    mode: Literal["legacy-live"] = "legacy-live"
    result_row: str
    live_phase: Literal["initial", "show-all", "complete"] = "initial"
    window_start: date | None = None
    window_end: date | None = None
    seen_references: tuple[str, ...] = ()


class ArunLiveCursor(FrozenModel):
    """Scope-bound query plan and its valid progress state."""

    mode: Literal["live"] = "live"
    scope: ArunDiscoveryScope
    plan: tuple[ArunQuery, ...] = Field(min_length=1)
    progress: ArunProgress
    search_form_evidence: EvidenceDigest | None = None

    @model_validator(mode="after")
    def progress_matches_plan(self) -> Self:
        """Require progress to describe one strict canonical-plan prefix."""
        progress = self.progress
        completed = progress.completed
        expected_keys = tuple(query.key for query in self.plan[: len(completed)])
        if tuple(item.key for item in completed) != expected_keys:
            message = "Arun completed queries are not a plan prefix"
            raise ValueError(message)
        expected_seen = _completed_references(completed)
        if progress.seen_references != expected_seen:
            message = "Arun checkpoint references are not unique"
            raise ValueError(message)
        if isinstance(progress, ArunComplete):
            if len(completed) != len(self.plan):
                message = "Arun terminal progress does not cover the plan"
                raise ValueError(message)
            return self
        if progress.next_query != len(completed) or progress.next_query >= len(
            self.plan
        ):
            message = "Arun next query does not follow the completed prefix"
            raise ValueError(message)
        if isinstance(progress, ArunAwaitingShowAll) and len(
            progress.initial_references
        ) != len(set(progress.initial_references)):
            message = "Arun first-page references are not unique"
            raise ValueError(message)
        return self


class ArunCheckpointV1(FrozenModel):
    """Discriminated fixture or live Arun checkpoint."""

    cursor: Annotated[
        ArunFixtureCursor | ArunLegacyLiveCursor | ArunLiveCursor,
        Field(discriminator="mode"),
    ]

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_v1(cls, value: object) -> object:
        """Upconvert checkpoints written before the cursor discriminator existed."""
        if (
            not isinstance(value, dict)
            or "cursor" in value
            or "result_row" not in value
        ):
            return value
        legacy = dict(value)
        is_live = (
            legacy.get("result_row") == "live"
            or legacy.get("window_start") is not None
            or legacy.get("window_end") is not None
            or legacy.get("live_phase", "initial") != "initial"
            or bool(legacy.get("seen_references"))
        )
        if is_live:
            return {"cursor": {"mode": "legacy-live", **legacy}}
        return {
            "cursor": {
                "mode": "fixture",
                "result_row": legacy["result_row"],
            }
        }


class ArunDocumentV1(FrozenModel):
    """Arun-native document metadata without attachment content."""

    title: str = Field(min_length=1)
    url: HttpUrl
    published_date: date | None = None
    document_type: str | None = Field(default=None, min_length=1)
    description: str | None = None
    source_links: tuple[HttpUrl, ...] = ()


class ArunApplicationV1(FrozenModel):
    """Arun-native Ocella application."""

    ocella_reference: str
    proposal_text: str
    decision_status: str
    parish_name: str | None
    documents: tuple[ArunDocumentV1, ...]
    site_address: str | None = None
    application_type: str | None = None
    received_date: date | None = None
    validated_date: date | None = None
    decision_by_date: date | None = None
    comment_by_date: date | None = None
    target_committee_date: date | None = None
    decision_date: date | None = None
    case_officer: str | None = None
    applicant: str | None = None
    agent: str | None = None
    appeal_reference: str | None = None
    appeal_status: str | None = None
    appeal_lodged_date: date | None = None
    appeal_decision_date: date | None = None


class ArunSearchForm(FrozenModel):
    """Validated portal-owned search form."""

    action: HttpUrl
    fields: tuple[FormField, ...]
    submit: FormField


class ArunShowAllForm(FrozenModel):
    """Validated result-owned expansion form."""

    action: HttpUrl
    fields: tuple[FormField, ...]


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
        if checkpoint is None:
            row = "first"
        elif isinstance(checkpoint.cursor, ArunFixtureCursor):
            row = checkpoint.cursor.result_row
        else:
            raise ArunCheckpointError
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
            next_checkpoint=ArunCheckpointV1(
                cursor=ArunFixtureCursor(result_row=next_row)
            ),
            complete=next_row == "complete",
        )

    async def _discover_live(  # noqa: C901, PLR0912, PLR0915
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: ArunCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[ArunCheckpointV1]]:
        scope = ArunDiscoveryScope.model_validate(window.model_dump())
        plan = _canonical_query_plan(scope)
        if checkpoint is None:
            cursor = ArunLiveCursor(
                scope=scope,
                plan=plan,
                progress=ArunReady(next_query=0),
            )
        elif isinstance(checkpoint.cursor, ArunLegacyLiveCursor):
            legacy = checkpoint.cursor
            if (
                legacy.window_start is not None and legacy.window_start != window.start
            ) or (legacy.window_end is not None and legacy.window_end != window.end):
                raise ArunCheckpointError
            cursor = ArunLiveCursor(
                scope=scope,
                plan=plan,
                progress=ArunReady(next_query=0),
            )
        elif isinstance(checkpoint.cursor, ArunLiveCursor):
            cursor = checkpoint.cursor
            if cursor.scope != scope or cursor.plan != plan:
                if not _is_complete(cursor.progress):
                    raise ArunCheckpointError
                cursor = ArunLiveCursor(
                    scope=scope,
                    plan=plan,
                    progress=ArunReady(next_query=0),
                )
        else:
            raise ArunCheckpointError
        if _is_complete(cursor.progress):
            yield DiscoveryBatch(
                references=(),
                next_checkpoint=ArunCheckpointV1(cursor=cursor),
                complete=True,
            )
            return
        form_request = PortalRequest(
            url=HttpUrl(_SEARCH_URL),
            intent=RequestIntent.SEARCH,
        )
        form_capture = await session.fetch(form_request)
        form = _parse_search_form(form_capture.body)
        cursor = cursor.model_copy(update={"search_form_evidence": form_capture.digest})
        pending_evidence: tuple[DiscoveryEvidenceCapture, ...] = (
            _discovery_evidence(form_capture, form_request),
        )
        while True:
            progress = cursor.progress
            if _is_complete(progress):
                return
            progress = cast("ArunReady | ArunAwaitingShowAll", progress)
            query = cursor.plan[progress.next_query]
            initial_request = _initial_search_request(form, query)
            initial_request_evidence = _request_evidence(initial_request)
            initial_capture = await session.fetch(initial_request)
            initial = _parse_search_results(initial_capture.body)
            query_evidence: tuple[DiscoveryEvidenceCapture, ...] = (
                *pending_evidence,
                _discovery_evidence(initial_capture, initial_request),
            )
            pending_evidence = ()
            if isinstance(progress, ArunAwaitingShowAll):
                replay_changed = (
                    initial.reported != progress.reported_count
                    or tuple(reference.reference for reference in initial.references)
                    != progress.initial_references
                )
                if replay_changed:
                    if (
                        initial.reported is None
                        or len(initial.references) == initial.reported
                    ):
                        fresh, _ = _fresh(
                            initial.references,
                            progress.seen_references,
                        )
                        cursor = _complete_query(
                            cursor,
                            _completed_query(
                                query,
                                initial.reported,
                                initial.references,
                                (initial_capture.digest, initial_request_evidence),
                                None,
                            ),
                        )
                        yield DiscoveryBatch(
                            references=fresh,
                            next_checkpoint=ArunCheckpointV1(cursor=cursor),
                            complete=isinstance(cursor.progress, ArunComplete),
                            evidence=query_evidence,
                            evidence_key=query.key,
                            evidence_page=1,
                        )
                        continue
                    progress = ArunAwaitingShowAll(
                        next_query=progress.next_query,
                        completed=progress.completed,
                        reported_count=initial.reported,
                        initial_references=tuple(
                            reference.reference for reference in initial.references
                        ),
                        initial_evidence=initial_capture.digest,
                        initial_request=initial_request_evidence,
                        seen_references=progress.seen_references,
                    )
                    cursor = cursor.model_copy(update={"progress": progress})
                    yield DiscoveryBatch(
                        references=(),
                        next_checkpoint=ArunCheckpointV1(cursor=cursor),
                        complete=False,
                        evidence=query_evidence,
                        evidence_key=query.key,
                        evidence_page=1,
                    )
                    query_evidence = ()
                else:
                    progress = progress.model_copy(
                        update={
                            "initial_evidence": initial_capture.digest,
                            "initial_request": initial_request_evidence,
                        }
                    )
                    cursor = cursor.model_copy(update={"progress": progress})
            else:
                if (
                    initial.reported is None
                    or len(initial.references) == initial.reported
                ):
                    fresh, _ = _fresh(
                        initial.references,
                        progress.seen_references,
                    )
                    cursor = _complete_query(
                        cursor,
                        _completed_query(
                            query,
                            initial.reported,
                            initial.references,
                            (initial_capture.digest, initial_request_evidence),
                            None,
                        ),
                    )
                    yield DiscoveryBatch(
                        references=fresh,
                        next_checkpoint=ArunCheckpointV1(cursor=cursor),
                        complete=isinstance(cursor.progress, ArunComplete),
                        evidence=query_evidence,
                        evidence_key=query.key,
                        evidence_page=1,
                    )
                    continue
                progress = ArunAwaitingShowAll(
                    next_query=progress.next_query,
                    completed=progress.completed,
                    reported_count=initial.reported,
                    initial_references=tuple(
                        reference.reference for reference in initial.references
                    ),
                    initial_evidence=initial_capture.digest,
                    initial_request=initial_request_evidence,
                    seen_references=progress.seen_references,
                )
                cursor = cursor.model_copy(update={"progress": progress})
                yield DiscoveryBatch(
                    references=(),
                    next_checkpoint=ArunCheckpointV1(cursor=cursor),
                    complete=False,
                    evidence=query_evidence,
                    evidence_key=query.key,
                    evidence_page=1,
                )
                query_evidence = ()
            expanded_request = _show_all_request(initial.show_all_form, query)
            expanded_request_evidence = _request_evidence(expanded_request)
            expanded_capture = await session.fetch(expanded_request)
            expanded = _parse_search_results(expanded_capture.body)
            if (
                expanded.has_show_all
                or expanded.reported not in (None, progress.reported_count)
                or len(expanded.references) != progress.reported_count
            ):
                raise ArunCountMismatchError(
                    progress.reported_count,
                    len(expanded.references),
                )
            fresh, _ = _fresh(
                expanded.references,
                progress.seen_references,
            )
            cursor = _complete_query(
                cursor,
                _completed_query(
                    query,
                    progress.reported_count,
                    expanded.references,
                    (
                        progress.initial_evidence,
                        cast("ArunRequestContract", progress.initial_request),
                    ),
                    (expanded_capture.digest, expanded_request_evidence),
                ),
            )
            yield DiscoveryBatch(
                references=fresh,
                next_checkpoint=ArunCheckpointV1(cursor=cursor),
                complete=isinstance(cursor.progress, ArunComplete),
                evidence=(
                    *query_evidence,
                    _discovery_evidence(expanded_capture, expanded_request),
                ),
                evidence_key=query.key,
                evidence_page=1,
            )

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
            ArunDocumentV1(
                title=unescape(title),
                url=HttpUrl(document_url),
                document_type="fixture",
                source_links=(HttpUrl(document_url),),
            )
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
                documents=collection_state(len(payload.documents)),
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
        if reference.locator is None:
            url = (
                f"{BASE_URL}/planningDetails?reference="
                f"{quote(reference.reference, safe='')}&from=planningSearch"
            )
        else:
            try:
                url = _validated_detail_locator(
                    reference.locator,
                    reference.reference,
                )
            except ArunParseError as error:
                raise ArunRoutingError(reference.reference) from error
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        _published_reference(
            _parse_labelled_fields(detail.body),
            reference.reference,
        )
        document_index = await session.fetch(
            _document_request(detail.body, reference.reference)
        )
        payload = _parse_application_pages(
            detail.body,
            document_index.body,
            reference.reference,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(payload.documents)),
                comments=UnavailableSection(
                    reason="Ocella does not expose a bounded comment text index"
                ),
            ),
            evidence=(detail, document_index),
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
            status="-".join(payload.decision_status.casefold().split()),
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
            normaliser_version="arun-v5",
            metadata=ApplicationMetadata(
                application_type=payload.application_type,
                decision=(
                    payload.decision_status
                    if payload.decision_date is not None
                    else None
                ),
                address=payload.site_address,
                received_date=payload.received_date,
                validated_date=payload.validated_date,
                decision_date=payload.decision_date,
                published_parties=tuple(
                    value for value in (payload.applicant, payload.agent) if value
                ),
                officer_name=payload.case_officer,
                source_url=snapshot.evidence[0].url,
                events=tuple(
                    event
                    for event in (
                        _application_event("decision-due", payload.decision_by_date),
                        _application_event(
                            "comment-deadline",
                            payload.comment_by_date,
                        ),
                        _application_event(
                            "target-committee",
                            payload.target_committee_date,
                        ),
                        _application_event(
                            "appeal-lodged",
                            payload.appeal_lodged_date,
                        ),
                        _application_event(
                            "appeal-decision",
                            payload.appeal_decision_date,
                            details=payload.appeal_status,
                        ),
                    )
                    if event is not None
                ),
                relationships=(
                    ()
                    if payload.appeal_reference is None
                    else (
                        ApplicationRelationship(
                            related_reference=payload.appeal_reference,
                            relationship_type="appeal",
                        ),
                    )
                ),
            ),
        )


class _SearchResults(FrozenModel):
    references: tuple[SourceReference, ...]
    reported: int | None
    show_all_form: ArunShowAllForm | None = None

    @property
    def has_show_all(self) -> bool:
        return self.show_all_form is not None


def _parse_search_form(body: bytes) -> ArunSearchForm:
    soup = BeautifulSoup(body, "html.parser")
    forms = tuple(soup.select('form[action="planningSearch"]'))
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        _raise_parse("planning search form")
    form = forms[0]
    if str(form.get("method", "")).casefold() != "post":
        _raise_parse("planning search form method")
    fields = _form_fields(form)
    names = tuple(field.name for field in fields)
    if any(names.count(name) != 1 for name in _QUERY_FIELDS):
        _raise_parse("planning search controls")
    submit_controls = tuple(
        FormField(
            name=str(control.get("name", "")),
            value=str(control.get("value", "")),
        )
        for control in form.select('input[type="submit"]')
        if control.get("name") == "action" and control.get("value") == "Search"
    )
    if len(submit_controls) != 1:
        _raise_parse("planning search submit")
    return ArunSearchForm(
        action=HttpUrl(urljoin(f"{BASE_URL}/", str(form.get("action")))),
        fields=fields,
        submit=submit_controls[0],
    )


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


def _query_values(query: ArunQuery) -> dict[str, str]:
    values = {
        "reference": "",
        "location": "",
        "OcellaPlanningSearch.postcode": "",
        "area": "",
        "applicant": "",
        "agent": "",
        "undecided": "",
        "type": "",
        "receivedFrom": "",
        "receivedTo": "",
        "decidedFrom": "",
        "decidedTo": "",
    }
    if isinstance(query, ArunDecidedQuery):
        values["decidedFrom"] = query.start.strftime("%d-%m-%y")
        values["decidedTo"] = query.end.strftime("%d-%m-%y")
    else:
        values["receivedFrom"] = query.start.strftime("%d-%m-%y")
        values["receivedTo"] = query.end.strftime("%d-%m-%y")
    if isinstance(query, ArunOpenReceivedQuery):
        values["undecided"] = "Y"
    return values


def _initial_search_request(
    form: ArunSearchForm,
    query: ArunQuery,
) -> PortalRequest:
    values = _query_values(query)
    fields = (
        *(
            FormField(name=field.name, value=values.get(field.name, field.value))
            for field in form.fields
        ),
        form.submit,
    )
    return PortalRequest(
        url=form.action,
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=fields,
    )


def _show_all_request(
    form: ArunShowAllForm | None,
    query: ArunQuery,
) -> PortalRequest:
    if form is None:
        raise ArunQueryReplayError
    if str(form.action).rstrip("/") != _SEARCH_URL:
        raise ArunQueryReplayError
    expected = _query_values(query)
    actual: dict[str, str] = {}
    for field in form.fields:
        if field.name in actual:
            raise ArunQueryReplayError
        actual[field.name] = field.value
    if any(actual.get(name) != value for name, value in expected.items()):
        raise ArunQueryReplayError
    if actual.get("action") != "Search" or actual.get("showall") != "showall":
        raise ArunQueryReplayError
    return PortalRequest(
        url=form.action,
        intent=RequestIntent.SEARCH,
        method=RequestMethod.POST,
        form=form.fields,
    )


def _request_evidence(request: PortalRequest) -> ArunRequestContract:
    return ArunRequestContract(
        url=request.url,
        method=request.method,
        form=request.form,
    )


def _discovery_evidence(
    capture: EvidenceCapture,
    request: PortalRequest,
) -> DiscoveryEvidenceCapture:
    return DiscoveryEvidenceCapture(
        capture=capture,
        request_url=request.url,
        request_method=request.method.value,
        request_form=tuple((field.name, field.value) for field in request.form),
    )


def _parse_search_results(
    body: bytes,
) -> _SearchResults:
    soup = BeautifulSoup(body, "html.parser")
    if soup.select_one('[class*="pagination"], a[rel="next"]') is not None:
        _raise_parse("result pagination")
    found = _parse_result_references(soup)
    show_all_form = _parse_show_all_form(soup)
    text = soup.get_text(" ", strip=True)
    reported = _parse_reported_count(
        soup,
        text,
        len(found),
        has_show_all=show_all_form is not None,
    )
    return _SearchResults(
        references=found,
        reported=reported,
        show_all_form=show_all_form,
    )


def _parse_result_references(  # noqa: C901
    soup: BeautifulSoup,
) -> tuple[SourceReference, ...]:
    result_tables = tuple(
        table
        for table in soup.select("table")
        if tuple(
            _normalise_label(header.get_text(" ", strip=True))
            for header in table.select("th")
        )
        == ("reference", "location", "proposal", "status")
    )
    all_links = tuple(soup.select('a[href*="planningDetails"]'))
    if not result_tables:
        if all_links:
            _raise_parse("result table")
        return ()
    if len(result_tables) != 1:
        _raise_parse("result table")
    table = result_tables[0]
    table_links = tuple(table.select('a[href*="planningDetails"]'))
    if len(table_links) != len(all_links):
        _raise_parse("result reference outside table")
    found = []
    seen = set()
    for row in table.select("tr:has(td)"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != _RESULT_COLUMNS:
            _raise_parse("result row")
        links = tuple(row.select("a"))
        if len(links) != 1 or links[0] not in cells[0].select("a"):
            _raise_parse("result reference")
        link = links[0]
        href = str(link.get("href", ""))
        if "planningDetails" not in href:
            _raise_parse("result reference")
        resolved = urljoin(f"{BASE_URL}/", href)
        parts = urlsplit(resolved)
        values = parse_qs(parts.query, keep_blank_values=True)
        references = values.get("reference", [])
        if len(references) != 1 or not references[0]:
            _raise_parse("result reference")
        reference = references[0]
        if unescape(link.get_text(" ", strip=True)) != reference:
            _raise_parse("result reference label")
        locator = _validated_detail_locator(resolved, reference)
        if reference in seen:
            _raise_parse("duplicate result reference")
        seen.add(reference)
        found.append(
            SourceReference(
                source_id=SOURCE,
                reference=reference,
                locator=locator,
            )
        )
    return tuple(found)


def _validated_detail_locator(locator: str, expected_reference: str) -> str:
    resolved = urljoin(f"{BASE_URL}/", locator)
    parts = urlsplit(resolved)
    base = urlsplit(BASE_URL)
    query = parse_qs(parts.query, keep_blank_values=True)
    allowed_queries = (
        {"reference": [expected_reference]},
        {"reference": [expected_reference], "from": ["planningSearch"]},
    )
    if (
        parts.scheme != "https"
        or parts.netloc != base.netloc
        or parts.path != f"{base.path}/planningDetails"
        or parts.fragment
        or query not in allowed_queries
    ):
        _raise_parse("result reference")
    return resolved


def _parse_reported_count(  # noqa: RET503
    soup: BeautifulSoup,
    text: str,
    reference_count: int,
    *,
    has_show_all: bool,
) -> int | None:
    if "retrieve more than 200 results" in text.casefold():
        raise ArunResultCapError
    partial_counts = tuple(
        match
        for element in soup.select("strong")
        if (
            match := re.fullmatch(
                r"First\s+(\d+)\s+results\s+shown,\s+there\s+are\s+(\d+)\s+in\s+total",
                element.get_text(" ", strip=True),
                re.IGNORECASE,
            )
        )
    )
    if len(partial_counts) > 1:
        _raise_parse("reported result count")
    if partial_counts:
        displayed = int(partial_counts[0].group(1))
        reported = int(partial_counts[0].group(2))
        if reported >= _RESULT_CAP:
            raise ArunResultCapError
        if (
            not has_show_all
            or displayed != reference_count
            or displayed < 1
            or displayed >= reported
        ):
            _raise_parse("partial result count")
        return reported
    if has_show_all:
        _raise_parse("reported result count")
    if _is_explicit_empty_result_page(soup, reference_count):
        return 0
    if _is_explicit_complete_result_page(soup, reference_count):
        if reference_count >= _RESULT_CAP:
            raise ArunResultCapError
        return None
    _raise_parse("reported result count")


def _is_explicit_empty_result_page(
    soup: BeautifulSoup,
    reference_count: int,
) -> bool:
    forms = tuple(
        form
        for form in soup.select(
            'form[name="OcellaPlanningSearch"][action="planningSearch"]'
        )
        if isinstance(form, Tag) and str(form.get("method", "")).casefold() == "post"
    )
    if len(forms) != 1 or reference_count != 0:
        return False
    try:
        _parse_search_form(soup.encode())
    except ArunParseError:
        return False
    messages = tuple(
        message
        for message in forms[0].select("span")
        if _normalise_label(message.get_text(" ", strip=True))
        == "no applications found for entered search criteria"
        and str(message.get("style", "")).replace(" ", "").casefold() == "color:maroon"
    )
    return len(messages) == 1


def _is_explicit_complete_result_page(
    soup: BeautifulSoup,
    reference_count: int,
) -> bool:
    forms = tuple(soup.select('form[name="search"][action="planningSearch"]'))
    if len(forms) != 1 or not isinstance(forms[0], Tag):
        return False
    form = forms[0]
    back_controls = tuple(
        control
        for control in form.select('input[type="submit"]')
        if control.get("name") == "BackToSearch"
        and control.get("value") == "Back to Search page"
    )
    result_tables = tuple(
        table
        for table in soup.select("table")
        if tuple(
            _normalise_label(header.get_text(" ", strip=True))
            for header in table.select("th")
        )
        == ("reference", "location", "proposal", "status")
    )
    rows = result_tables[0].select("tr:has(td)") if result_tables else ()
    return (
        reference_count > 0
        and str(form.get("method", "")).casefold() == "post"
        and len(back_controls) == 1
        and len(result_tables) == 1
        and len(rows) == reference_count
        and all(
            len(row.find_all("td", recursive=False)) == _RESULT_COLUMNS for row in rows
        )
    )


def _parse_show_all_form(soup: BeautifulSoup) -> ArunShowAllForm | None:
    show_all_forms = tuple(
        form
        for form in soup.select("form")
        if isinstance(form, Tag)
        and form.select_one('input[name="showall"][value="showall"]') is not None
    )
    if len(show_all_forms) > 1:
        _raise_parse("show all form")
    show_all_form = None
    if show_all_forms:
        form = show_all_forms[0]
        if str(form.get("method", "")).casefold() != "post":
            _raise_parse("show all form method")
        show_all_form = ArunShowAllForm(
            action=HttpUrl(urljoin(f"{BASE_URL}/", str(form.get("action", "")))),
            fields=_form_fields(form),
        )
    return show_all_form


def _document_request(body: bytes, expected_reference: str) -> PortalRequest:
    soup = BeautifulSoup(body, "html.parser")
    forms = tuple(
        form
        for form in soup.select("form[action]")
        if urlsplit(urljoin(f"{BASE_URL}/", str(form.get("action", "")))).path.endswith(
            "/showDocuments"
        )
    )
    if len(forms) != 1:
        _raise_parse("document action")
    form = forms[0]
    action = urljoin(f"{BASE_URL}/", str(form.get("action", "")))
    parts = urlsplit(action)
    base = urlsplit(BASE_URL)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        str(form.get("method", "")).casefold() != "post"
        or parts.scheme != "https"
        or parts.netloc != base.netloc
        or parts.path != f"{base.path}/showDocuments"
        or parts.fragment
        or set(query) != {"reference", "module"}
        or query.get("module") != ["pl"]
    ):
        _raise_parse("document action")
    if query.get("reference") != [expected_reference]:
        _raise_parse("document action reference")
    submits = tuple(
        FormField(name=str(control.get("name")), value=str(control.get("value")))
        for control in form.select('input[type="submit"]')
        if control.get("name") == "ViewDocuments"
        and control.get("value") == "View Documents"
    )
    if len(submits) != 1:
        _raise_parse("document action submit")
    return PortalRequest(
        url=HttpUrl(action),
        intent=RequestIntent.DETAIL,
        method=RequestMethod.POST,
        form=submits,
    )


def _parse_document_index(  # noqa: C901
    body: bytes,
) -> tuple[ArunDocumentV1, ...]:
    soup = BeautifulSoup(body, "html.parser")
    if soup.select_one('[class*="pagination"], a[rel="next"]') is not None:
        _raise_parse("document pagination")
    selected_types = tuple(soup.select('select[name="selectedtype"]'))
    if len(selected_types) != 1:
        _raise_parse("document filter")
    selected_options = tuple(selected_types[0].select("option[selected]"))
    options = tuple(selected_types[0].select("option"))
    if len(selected_options) > 1 or not options:
        _raise_parse("document filter")
    effective = selected_options[0] if selected_options else options[0]
    if str(effective.get("value", "")):
        _raise_parse("document filter")
    empty_markers = _document_empty_markers(soup)
    if _is_explicit_empty_document_page(soup, empty_markers):
        return ()
    if empty_markers:
        _raise_parse("document empty state")
    tables = tuple(
        table
        for table in soup.select("table")
        if table.select_one('a[href*="viewDocument"]') is not None
        or {"type", "date"}.issubset(
            {
                _normalise_label(header.get_text(" ", strip=True))
                for header in table.select("th")
            }
        )
    )
    if not tables:
        _raise_parse("document table")
    if len(tables) != 1:
        _raise_parse("document table")
    all_links = tuple(soup.select('a[href*="viewDocument"]'))
    table_links = tuple(tables[0].select('a[href*="viewDocument"]'))
    if len(all_links) != len(table_links) or any(
        link not in table_links for link in all_links
    ):
        _raise_parse("document link outside table")
    documents = []
    for row in tables[0].select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        documents.append(_parse_document_row(cells))
    if not documents:
        _raise_parse("document rows")
    return tuple(documents)


def _document_empty_markers(soup: BeautifulSoup) -> tuple[Tag, ...]:
    markers = []
    for table in soup.select("table"):
        rows = table.find_all("tr", recursive=False)
        if len(rows) != 1:
            continue
        cells = rows[0].find_all("td", recursive=False)
        if (
            len(cells) == 1
            and _normalise_label(cells[0].get_text(" ", strip=True))
            == "there are no documents for this section"
        ):
            markers.append(table)
    return tuple(markers)


def _is_explicit_empty_document_page(
    soup: BeautifulSoup,
    markers: tuple[Tag, ...],
) -> bool:
    headings = tuple(
        heading
        for heading in soup.select("strong, h1, h2, h3, h4, h5, h6")
        if _normalise_label(heading.get_text(" ", strip=True)) == "documents"
    )
    return (
        bool(headings)
        and len(markers) == 1
        and soup.select_one('a[href*="viewDocument"]') is None
        and not any(
            {"type", "date"}.issubset(
                {
                    _normalise_label(header.get_text(" ", strip=True))
                    for header in table.select("th")
                }
            )
            for table in soup.select("table")
        )
    )


def _parse_document_row(cells: list[Tag]) -> ArunDocumentV1:
    if len(cells) != _DOCUMENT_COLUMNS:
        _raise_parse("document row")
    links = tuple(link for cell in cells for link in cell.select("a"))
    if len(links) != 1 or links[0] not in cells[0].select("a"):
        _raise_parse("document link")
    link = links[0]
    if "viewDocument" not in str(link.get("href", "")):
        _raise_parse("document link")
    url = HttpUrl(urljoin(f"{BASE_URL}/", str(link.get("href", ""))))
    parts = urlsplit(str(url))
    base = urlsplit(BASE_URL)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        parts.scheme != "https"
        or parts.netloc != base.netloc
        or parts.path != f"{base.path}/viewDocument"
        or parts.fragment
        or set(query) != {"file", "module"}
        or len(query.get("file", [])) != 1
        or not query["file"][0]
        or query.get("module") != ["pl"]
    ):
        _raise_parse("document link")
    document_type = unescape(cells[0].get_text(" ", strip=True))
    description = unescape(cells[4].get_text(" ", strip=True)) or None
    return ArunDocumentV1(
        title=description or document_type,
        url=url,
        published_date=_optional_date_text(cells[2].get_text(" ", strip=True)),
        document_type=document_type,
        description=description,
        source_links=(url,),
    )


def _parse_labelled_fields(body: bytes) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    fields: dict[str, str] = {}
    for table in soup.select("table"):
        rows = tuple(table.find_all("tr", recursive=False))
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < _MINIMUM_LABELLED_CELLS:
                continue
            label = _normalise_label(cells[0].get_text(" ", strip=True))
            if label == "appeal":
                break
            _store_unique_field(
                fields,
                label,
                cells[-1].get_text(" ", strip=True),
            )
    for term in soup.select("dt"):
        value = term.find_next_sibling("dd")
        if isinstance(value, Tag):
            _store_unique_field(
                fields,
                _normalise_label(term.get_text(" ", strip=True)),
                value.get_text(" ", strip=True),
            )
    if not fields:
        _raise_parse("labelled detail fields")
    return fields


def _store_unique_field(fields: dict[str, str], label: str, value: str) -> None:
    if label in fields:
        _raise_parse("duplicate labelled detail field")
    fields[label] = value


class _ArunAppealFields(FrozenModel):
    reference: str | None = None
    status: str | None = None
    lodged_date: date | None = None
    decision_date: date | None = None


def _parse_appeal_fields(body: bytes) -> _ArunAppealFields:
    soup = BeautifulSoup(body, "html.parser")
    candidates: list[tuple[Tag, int]] = []
    for table in soup.select("table"):
        rows = tuple(table.find_all("tr", recursive=False))
        for index, row in enumerate(rows):
            cells = row.find_all(["th", "td"], recursive=False)
            if (
                len(cells) >= _MINIMUM_LABELLED_CELLS
                and _normalise_label(cells[0].get_text(" ", strip=True)) == "appeal"
            ):
                candidates.append((table, index))
    if not candidates:
        return _ArunAppealFields()
    if len(candidates) != 1:
        _raise_parse("appeal block")
    table, start = candidates[0]
    table_rows = tuple(table.find_all("tr", recursive=False))
    if start + 4 != len(table_rows):
        _raise_parse("appeal block")
    rows = table_rows[start : start + 4]
    labels_and_values = []
    for row in rows:
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < _MINIMUM_LABELLED_CELLS:
            _raise_parse("appeal block")
        labels_and_values.append(
            (
                _normalise_label(cells[0].get_text(" ", strip=True)),
                cells[-1].get_text(" ", strip=True),
            )
        )
    if tuple(label for label, _value in labels_and_values) != (
        "appeal",
        "lodged",
        "type",
        "decision",
    ):
        _raise_parse("appeal block")
    values = dict(labels_and_values)
    return _ArunAppealFields(
        reference=unescape(values["appeal"]) or None,
        status=unescape(values["type"]) or None,
        lodged_date=_optional_date_text(values["lodged"]),
        decision_date=_optional_date_text(values["decision"]),
    )


def _parse_application_pages(
    detail_body: bytes,
    document_body: bytes,
    expected_reference: str,
) -> ArunApplicationV1:
    fields = _parse_labelled_fields(detail_body)
    appeal = _parse_appeal_fields(detail_body)
    published = _published_reference(fields, expected_reference)
    return ArunApplicationV1(
        ocella_reference=published,
        proposal_text=_required_field(fields, "proposal", "description"),
        decision_status=_required_field(fields, "status"),
        parish_name=_optional_field(fields, "parish"),
        documents=_parse_document_index(document_body),
        site_address=_optional_field(fields, "location", "address"),
        application_type=_optional_field(fields, "application type"),
        received_date=_optional_date(fields, "received", "received date"),
        validated_date=_optional_date(fields, "validated", "validated date"),
        decision_by_date=_optional_date(fields, "decision by"),
        comment_by_date=_optional_date(fields, "comment by"),
        target_committee_date=_optional_date(fields, "target cmte"),
        decision_date=_optional_date(fields, "decided", "decision date"),
        case_officer=_optional_field(fields, "case officer"),
        applicant=_optional_field(fields, "applicant"),
        agent=_optional_field(fields, "agent"),
        appeal_reference=appeal.reference,
        appeal_status=appeal.status,
        appeal_lodged_date=appeal.lodged_date,
        appeal_decision_date=appeal.decision_date,
    )


def _published_reference(fields: dict[str, str], expected_reference: str) -> str:
    published = _required_field(fields, "reference", "application reference")
    if published != expected_reference:
        raise ArunReferenceMismatchError(expected_reference, published)
    return published


def _fresh(
    references: tuple[SourceReference, ...], seen_values: tuple[str, ...]
) -> tuple[tuple[SourceReference, ...], tuple[str, ...]]:
    seen = set(seen_values)
    ordered_seen = list(seen_values)
    fresh = []
    for reference in references:
        if reference.reference not in seen:
            seen.add(reference.reference)
            ordered_seen.append(reference.reference)
            fresh.append(reference)
    return tuple(fresh), tuple(ordered_seen)


def _is_complete(progress: ArunProgress) -> bool:
    return progress.kind == "complete"


def _complete_query(
    cursor: ArunLiveCursor,
    summary: ArunCompletedQuery,
) -> ArunLiveCursor:
    progress = cursor.progress
    if isinstance(progress, ArunComplete):
        raise ArunCheckpointError
    completed = (
        *progress.completed,
        summary,
    )
    seen_references = _completed_references(completed)
    next_query = progress.next_query + 1
    next_progress: ArunProgress
    if next_query == len(cursor.plan):
        next_progress = ArunComplete(
            completed=completed,
            seen_references=seen_references,
        )
    else:
        next_progress = ArunReady(
            next_query=next_query,
            completed=completed,
            seen_references=seen_references,
        )
    return ArunLiveCursor(
        scope=cursor.scope,
        plan=cursor.plan,
        progress=next_progress,
        search_form_evidence=cursor.search_form_evidence,
    )


def _completed_query(
    query: ArunQuery,
    reported_count: int | None,
    references: tuple[SourceReference, ...],
    initial: tuple[EvidenceDigest, ArunRequestContract],
    expanded: tuple[EvidenceDigest, ArunRequestContract] | None,
) -> ArunCompletedQuery:
    return ArunCompletedQuery(
        key=query.key,
        reported_count=reported_count,
        enumerated_count=len(references),
        references=tuple(reference.reference for reference in references),
        initial_evidence=initial[0],
        expanded_evidence=None if expanded is None else expanded[0],
        initial_request=initial[1],
        expanded_request=None if expanded is None else expanded[1],
    )


def _completed_references(
    completed: tuple[ArunCompletedQuery, ...],
) -> tuple[str, ...]:
    seen = set()
    ordered = []
    for item in completed:
        for reference in item.references:
            if reference not in seen:
                seen.add(reference)
                ordered.append(reference)
    return tuple(ordered)


def _application_event(
    event_type: str,
    event_date: date | None,
    *,
    details: str | None = None,
) -> ApplicationEvent | None:
    if event_date is None:
        return None
    return ApplicationEvent(
        event_type=event_type,
        event_at=datetime.combine(event_date, datetime.min.time(), tzinfo=UTC),
        details=details,
    )


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


def _optional_date_text(value: str) -> date | None:
    if not value:
        return None
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return _raise_parse("document date")


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


class ArunResultCapError(ValueError):
    """One query reached the portal's non-enumerable result cap."""

    def __init__(self) -> None:
        """Expose a stable boundary failure without response content."""
        super().__init__("Arun query reached the 200-result portal cap")


class ArunQueryReplayError(ValueError):
    """A result-owned Show All form does not match its active query."""

    def __init__(self) -> None:
        """Expose a stable replay failure without query content."""
        super().__init__("Arun Show All form does not match the active query")


class ArunCheckpointError(ValueError):
    """A saved cursor belongs to another received-date window."""


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
