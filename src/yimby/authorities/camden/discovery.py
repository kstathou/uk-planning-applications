# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden-owned GeneralSearch query and checkpoint semantics."""

from __future__ import annotations

from datetime import date  # noqa: TC003 - Pydantic resolves this annotation at runtime.
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, RootModel, model_validator

from yimby.domain import DiscoveryWindow, FrozenModel

GENERAL_SEARCH_URL = (
    "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/GeneralSearch.aspx"
)


class _CamdenModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CamdenDateField(StrEnum):
    """Official Camden date fields used by the bounded bootstrap."""

    RECEIVED = "DATE_RECEIVED"
    VALID = "DATE_VALID"
    DECISION = "DATE_DECISION"


class CamdenOpenStatus(StrEnum):
    """Official Camden statuses that remain source-owned and active."""

    REGISTERED = "4"
    APPEAL_LODGED = "14"

    @property
    def label(self) -> str:
        """Return the official visible label for the source code."""
        return {
            CamdenOpenStatus.REGISTERED: "REGISTERED",
            CamdenOpenStatus.APPEAL_LODGED: "APPEAL LODGED",
        }[self]


class CamdenDiscoveryScopeV1(_CamdenModel):
    """Semantic live scope bound into every durable checkpoint."""

    start: date
    end: date
    include_open: bool

    @model_validator(mode="after")
    def _ordered_dates(self) -> Self:
        if self.end < self.start:
            message = "Camden discovery end date precedes start date"
            raise ValueError(message)
        return self

    @classmethod
    def from_window(cls, window: DiscoveryWindow) -> Self:
        """Copy the common window into Camden-owned durable state."""
        return cls(
            start=window.start,
            end=window.end,
            include_open=window.include_open,
        )


class CamdenDateQueryV1(_CamdenModel):
    """One official inclusive Camden date-range query."""

    kind: Literal["date"] = "date"
    field: CamdenDateField
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered_dates(self) -> Self:
        if self.end < self.start:
            message = "Camden query end date precedes start date"
            raise ValueError(message)
        return self

    @property
    def key(self) -> str:
        """Return a stable semantic query identity."""
        return f"date:{self.field}:{self.start.isoformat()}:{self.end.isoformat()}"


class CamdenStatusQueryV1(_CamdenModel):
    """One official Camden active-status query."""

    kind: Literal["status"] = "status"
    status: CamdenOpenStatus

    @property
    def key(self) -> str:
        """Return a stable semantic query identity."""
        return f"status:{self.status}:{self.status.label}"


CamdenDiscoveryQueryV1 = Annotated[
    CamdenDateQueryV1 | CamdenStatusQueryV1,
    Field(discriminator="kind"),
]


def camden_query_inventory(
    scope: CamdenDiscoveryScopeV1,
) -> tuple[CamdenDiscoveryQueryV1, ...]:
    """Return the exact ordered query inventory for a live scope."""
    queries: list[CamdenDiscoveryQueryV1] = [
        CamdenDateQueryV1(
            field=field,
            start=scope.start,
            end=scope.end,
        )
        for field in (
            CamdenDateField.RECEIVED,
            CamdenDateField.VALID,
            CamdenDateField.DECISION,
        )
    ]
    if scope.include_open:
        queries.extend(
            CamdenStatusQueryV1(status=status)
            for status in (
                CamdenOpenStatus.REGISTERED,
                CamdenOpenStatus.APPEAL_LODGED,
            )
        )
    return tuple(queries)


class CamdenSeenReferenceV1(_CamdenModel):
    """One public reference paired with its stable Northgate key."""

    reference: str = Field(min_length=1)
    locator: str = Field(min_length=1, pattern=r"^\d+$")


class CamdenCompletedQueryV1(_CamdenModel):
    """Reconciled membership and source total for one finished query."""

    query: CamdenDiscoveryQueryV1
    reported_count: int = Field(ge=0)
    ordered_references: tuple[CamdenSeenReferenceV1, ...]

    @model_validator(mode="after")
    def _reconciled_membership(self) -> Self:
        _require_unique_references(
            self.ordered_references,
            context="completed query",
        )
        if len(self.ordered_references) != self.reported_count:
            message = "Camden completed query membership must equal its reported count"
            raise ValueError(message)
        return self


class CamdenBetweenQueriesV1(_CamdenModel):
    """Progress after one reconciled query and before the next POST."""

    kind: Literal["between-queries"] = "between-queries"
    next_query_index: int = Field(ge=0)


class CamdenPagingQueryV1(_CamdenModel):
    """Committed active-query prefix that must be replayed on resume."""

    kind: Literal["paging-query"] = "paging-query"
    query_index: int = Field(ge=0)
    reported_count: int = Field(gt=0)
    next_offset: int = Field(ge=10, multiple_of=10)
    ordered_prefix: tuple[CamdenSeenReferenceV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reconciled_prefix(self) -> Self:
        if len(self.ordered_prefix) != self.next_offset:
            message = "Camden next offset must equal ordered prefix length"
            raise ValueError(message)
        _require_unique_references(
            self.ordered_prefix,
            context="ordered prefix",
        )
        if self.next_offset >= self.reported_count:
            message = "Camden active query offset must precede reported total"
            raise ValueError(message)
        return self


class CamdenTerminalV1(_CamdenModel):
    """Terminal progress with no active query or wire state."""

    kind: Literal["terminal"] = "terminal"


CamdenLiveProgressV1 = Annotated[
    CamdenBetweenQueriesV1 | CamdenPagingQueryV1 | CamdenTerminalV1,
    Field(discriminator="kind"),
]


class CamdenFixtureCheckpointV1(_CamdenModel):
    """Legacy deterministic fixture cursor."""

    kind: Literal["fixture"] = "fixture"
    view_state_page: str = Field(min_length=1)


class CamdenLiveCheckpointV1(_CamdenModel):
    """Complete semantic progress for the Camden live query inventory."""

    kind: Literal["live"] = "live"
    schema_version: Literal[1] = 1
    scope: CamdenDiscoveryScopeV1
    query_inventory: tuple[CamdenDiscoveryQueryV1, ...] = Field(min_length=1)
    completed_queries: tuple[CamdenCompletedQueryV1, ...] = ()
    seen_references: tuple[CamdenSeenReferenceV1, ...] = ()
    progress: CamdenLiveProgressV1

    @model_validator(mode="after")
    def _coherent_progress(self) -> Self:
        expected_inventory = camden_query_inventory(self.scope)
        if self.query_inventory != expected_inventory:
            message = "Camden stored query inventory does not match its scope"
            raise ValueError(message)
        completed = self.completed_queries
        if len(completed) > len(expected_inventory):
            message = "Camden completed query count exceeds inventory"
            raise ValueError(message)
        completed_inventory = tuple(item.query for item in completed)
        if completed_inventory != expected_inventory[: len(completed)]:
            message = "Camden completed queries must be an exact inventory prefix"
            raise ValueError(message)
        active = _active_prefix(self.progress, len(completed), len(expected_inventory))
        union = _reference_union(
            tuple(
                reference
                for result in completed
                for reference in result.ordered_references
            )
            + active
        )
        if self.seen_references != union:
            message = "Camden seen references must equal the deterministic query union"
            raise ValueError(message)
        return self


CamdenCheckpointBranchV1 = Annotated[
    CamdenFixtureCheckpointV1 | CamdenLiveCheckpointV1,
    Field(discriminator="kind"),
]


class CamdenCheckpointV1(RootModel[CamdenCheckpointBranchV1]):
    """Versioned fixture or live Camden checkpoint boundary."""

    root: CamdenCheckpointBranchV1

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fixture(cls, value: object) -> object:
        """Accept only the exact pre-live fixture checkpoint shape."""
        if isinstance(value, dict) and set(value) == {"view_state_page"}:
            return {"kind": "fixture", "view_state_page": value["view_state_page"]}
        return value


def initial_live_checkpoint(window: DiscoveryWindow) -> CamdenCheckpointV1:
    """Create empty live progress bound to the requested scope."""
    scope = CamdenDiscoveryScopeV1.from_window(window)
    return CamdenCheckpointV1(
        root=CamdenLiveCheckpointV1(
            scope=scope,
            query_inventory=camden_query_inventory(scope),
            progress=CamdenBetweenQueriesV1(next_query_index=0),
        )
    )


def _require_unique_references(
    references: tuple[CamdenSeenReferenceV1, ...],
    *,
    context: str,
) -> None:
    by_reference: dict[str, str] = {}
    for item in references:
        if item.reference in by_reference:
            message = f"Camden {context} must contain unique references"
            raise ValueError(message)
        by_reference[item.reference] = item.locator


def _reference_union(
    references: tuple[CamdenSeenReferenceV1, ...],
) -> tuple[CamdenSeenReferenceV1, ...]:
    ordered: list[CamdenSeenReferenceV1] = []
    by_reference: dict[str, str] = {}
    for item in references:
        locator = by_reference.get(item.reference)
        if locator is None:
            by_reference[item.reference] = item.locator
            ordered.append(item)
        elif locator != item.locator:
            message = "Camden public reference changed Northgate locator"
            raise ValueError(message)
    return tuple(ordered)


def _active_prefix(
    progress: CamdenLiveProgressV1,
    completed_count: int,
    inventory_count: int,
) -> tuple[CamdenSeenReferenceV1, ...]:
    if isinstance(progress, CamdenTerminalV1):
        if completed_count != inventory_count:
            message = "Camden terminal checkpoint requires every query result"
            raise ValueError(message)
        return ()
    if isinstance(progress, CamdenBetweenQueriesV1):
        if progress.next_query_index != completed_count:
            message = "Camden next query index must follow completed queries"
            raise ValueError(message)
        if progress.next_query_index >= inventory_count:
            message = "Camden final query must transition to terminal"
            raise ValueError(message)
        return ()
    if progress.query_index != completed_count:
        message = "Camden active query must follow completed queries"
        raise ValueError(message)
    if progress.query_index >= inventory_count:
        message = "Camden active query is outside the inventory"
        raise ValueError(message)
    return progress.ordered_prefix
