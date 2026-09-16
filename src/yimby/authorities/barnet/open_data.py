# Copyright (c) 2026 Kostas Stathoulopoulos

"""Strict evidence schema for the official Barnet Open Data assessment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, StringConstraints

from yimby.domain import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _StrictFrozenModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class BarnetOpenDataSources(_StrictFrozenModel):
    """Official pages and response digests used for the assessment."""

    dataset_page: Literal[
        "https://open.barnet.gov.uk/dataset/planning-applications-2nq32"
    ]
    dataset_api: Literal["https://open.barnet.gov.uk/api/v3/dataset/2nq32"]
    catalog_api: Literal["https://open.barnet.gov.uk/api/v3/datasets/export.json"]
    datapress_api_documentation: Literal["https://datapress.com/docs/api/"]
    dataset_payload_sha256: _SHA256
    catalog_payload_sha256: _SHA256


class BarnetHistoricalPlanningDataset(_StrictFrozenModel):
    """The official annual decided-application resource collection."""

    dataset_id: Literal["2nq32"]
    title: Literal["Planning Applications"]
    updated_at: AwareDatetime
    resource_count: Literal[13]
    resource_format: Literal["csv"]
    resource_scope: Literal["decided-applications"]
    oldest_timeframe_from: Literal["1965-05"]
    newest_timeframe_to: Literal["2021-03"]
    newest_resource_id: Literal["xl9"]
    newest_resource_title: Literal["Planning Applications Decided 2020/2021"]
    live_link_count: Literal[0]


class BarnetDummyPlanningDocumentsDataset(_StrictFrozenModel):
    """A newer planning-named catalogue item that is not a collection source."""

    dataset_id: Literal["e1g8k"]
    title: Literal["Planning Applications Documents_POC (Internal use only_dummy data)"]
    updated_at: AwareDatetime
    resource_count: Literal[6]
    resource_format: Literal["pdf"]
    timeframe_present: Literal[False]
    live_link_count: Literal[0]
    classification: Literal["internal-dummy-document-proof-of-concept"]


class BarnetOpenDataCatalogAssessment(_StrictFrozenModel):
    """Sanitized result of searching the complete public catalogue metadata."""

    public_dataset_count: Literal[397]
    match_method: Literal["case-insensitive-planning-application-phrase"]
    matching_dataset_ids: tuple[Literal["2nq32"], Literal["e1g8k"]]
    newer_nonqualifying_dataset: BarnetDummyPlanningDocumentsDataset


class BarnetOpenDataCoverageDecision(_StrictFrozenModel):
    """Fail-closed coverage decision for the two required populations."""

    current_30_day_population: Literal[False]
    older_open_population: Literal[False]
    newer_qualifying_resource_found: Literal[False]
    decision: Literal["historical-fallback-insufficient"]
    preserved_live_blocker: Literal["official-http-429"]


class BarnetOpenDataSanitization(_StrictFrozenModel):
    """The disclosure boundary of the committed assessment."""

    metadata_only: Literal[True]
    omitted: tuple[
        Literal["resource-bodies"],
        Literal["attachment-bodies"],
        Literal["application-identities"],
        Literal["session-material"],
        Literal["nonmatching-catalog-records"],
    ]


class BarnetOpenDataAssessmentV1(_StrictFrozenModel):
    """Publishable proof that Open Barnet cannot replace the blocked register."""

    schema_version: Literal[1]
    authority_id: Literal["barnet"]
    observed_at: AwareDatetime
    sources: BarnetOpenDataSources
    planning_dataset: BarnetHistoricalPlanningDataset
    catalog: BarnetOpenDataCatalogAssessment
    coverage: BarnetOpenDataCoverageDecision
    sanitization: BarnetOpenDataSanitization


def load_barnet_open_data_assessment(path: Path) -> BarnetOpenDataAssessmentV1:
    """Load and strictly validate one committed Open Barnet assessment."""
    return BarnetOpenDataAssessmentV1.model_validate_json(
        path.read_text(encoding="utf-8")
    )


__all__ = ["BarnetOpenDataAssessmentV1", "load_barnet_open_data_assessment"]
