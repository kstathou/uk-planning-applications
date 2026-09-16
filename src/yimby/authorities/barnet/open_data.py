# Copyright (c) 2026 Kostas Stathoulopoulos

"""Strict evidence schema for the official Barnet Open Data assessment."""

from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
)

from yimby.domain import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _StrictFrozenModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _SourceModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)


class _DatasetTimeframe(_SourceModel):
    from_: str = Field(alias="from")
    to: str


class _DatasetResource(_SourceModel):
    title: str
    format: str
    timeframe: _DatasetTimeframe


class _DatasetSnapshot(_SourceModel):
    id: str
    title: str
    updated_at: AwareDatetime = Field(alias="updatedAt")
    resources: dict[str, _DatasetResource]
    links: dict[str, Any]


class _CatalogResource(_SourceModel):
    id: str
    title: str
    filename: str
    description: str
    timeframe_from: str | None = Field(alias="timeframeFrom")
    timeframe_to: str | None = Field(alias="timeframeTo")


class _CatalogDataset(_SourceModel):
    id: str
    title: str
    description: str
    updated_at: AwareDatetime = Field(alias="updatedAt")
    resources: tuple[_CatalogResource, ...]
    links: tuple[Any, ...]


_CATALOG_ADAPTER = TypeAdapter(tuple[_CatalogDataset, ...])


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


class BarnetOpenDataEvidenceError(RuntimeError):
    """Retained metadata cannot support the committed coverage assessment."""


def derive_barnet_open_data_assessment(
    dataset_payload: bytes,
    catalog_payload: bytes,
    *,
    observed_at: AwareDatetime,
) -> BarnetOpenDataAssessmentV1:
    """Derive the sanitized assessment from retained official API metadata."""
    dataset = _DatasetSnapshot.model_validate_json(dataset_payload)
    catalog = _CATALOG_ADAPTER.validate_json(catalog_payload, strict=True)
    catalog_by_id = {item.id: item for item in catalog}
    catalog_dataset = catalog_by_id["2nq32"]
    poc_dataset = catalog_by_id["e1g8k"]
    dataset_resources = tuple(dataset.resources.values())

    _require(
        condition=(
            set(dataset.resources)
            == {resource.id for resource in catalog_dataset.resources}
        ),
        code="dataset-catalog-resource-mismatch",
    )
    _require(
        condition=dataset.updated_at == catalog_dataset.updated_at,
        code="dataset-update-mismatch",
    )

    newest_resource_id, newest_resource = max(
        dataset.resources.items(), key=lambda item: item[1].timeframe.to
    )
    formats = {resource.format.casefold() for resource in dataset_resources}
    resource_format = formats.pop() if len(formats) == 1 else "mixed"
    resource_scope = (
        "decided-applications"
        if all(
            resource.title.startswith("Planning Applications Decided")
            for resource in dataset_resources
        )
        else "unclassified"
    )
    matching_ids = tuple(
        sorted(
            item.id
            for item in catalog
            if "planning application" in _catalog_search_text(item)
        )
    )
    poc_suffixes = {
        PurePosixPath(resource.filename).suffix.removeprefix(".").casefold()
        for resource in poc_dataset.resources
    }
    poc_format = poc_suffixes.pop() if len(poc_suffixes) == 1 else "mixed"

    return BarnetOpenDataAssessmentV1.model_validate(
        {
            "schema_version": 1,
            "authority_id": "barnet",
            "observed_at": observed_at,
            "sources": {
                "dataset_page": (
                    "https://open.barnet.gov.uk/dataset/planning-applications-2nq32"
                ),
                "dataset_api": "https://open.barnet.gov.uk/api/v3/dataset/2nq32",
                "catalog_api": (
                    "https://open.barnet.gov.uk/api/v3/datasets/export.json"
                ),
                "datapress_api_documentation": "https://datapress.com/docs/api/",
                "dataset_payload_sha256": sha256(dataset_payload).hexdigest(),
                "catalog_payload_sha256": sha256(catalog_payload).hexdigest(),
            },
            "planning_dataset": {
                "dataset_id": dataset.id,
                "title": dataset.title,
                "updated_at": dataset.updated_at,
                "resource_count": len(dataset_resources),
                "resource_format": resource_format,
                "resource_scope": resource_scope,
                "oldest_timeframe_from": min(
                    resource.timeframe.from_ for resource in dataset_resources
                ),
                "newest_timeframe_to": newest_resource.timeframe.to,
                "newest_resource_id": newest_resource_id,
                "newest_resource_title": newest_resource.title,
                "live_link_count": len(dataset.links),
            },
            "catalog": {
                "public_dataset_count": len(catalog),
                "match_method": "case-insensitive-planning-application-phrase",
                "matching_dataset_ids": matching_ids,
                "newer_nonqualifying_dataset": {
                    "dataset_id": poc_dataset.id,
                    "title": poc_dataset.title,
                    "updated_at": poc_dataset.updated_at,
                    "resource_count": len(poc_dataset.resources),
                    "resource_format": poc_format,
                    "timeframe_present": any(
                        resource.timeframe_from is not None
                        or resource.timeframe_to is not None
                        for resource in poc_dataset.resources
                    ),
                    "live_link_count": len(poc_dataset.links),
                    "classification": "internal-dummy-document-proof-of-concept",
                },
            },
            "coverage": {
                "current_30_day_population": False,
                "older_open_population": False,
                "newer_qualifying_resource_found": False,
                "decision": "historical-fallback-insufficient",
                "preserved_live_blocker": "official-http-429",
            },
            "sanitization": {
                "metadata_only": True,
                "omitted": (
                    "resource-bodies",
                    "attachment-bodies",
                    "application-identities",
                    "session-material",
                    "nonmatching-catalog-records",
                ),
            },
        }
    )


def verify_barnet_open_data_assessment(
    assessment: BarnetOpenDataAssessmentV1,
    dataset_payload: bytes,
    catalog_payload: bytes,
) -> None:
    """Verify every committed claim against the retained metadata payloads."""
    derived = derive_barnet_open_data_assessment(
        dataset_payload,
        catalog_payload,
        observed_at=assessment.observed_at,
    )
    _require(condition=derived == assessment, code="assessment-mismatch")


def load_barnet_open_data_assessment(path: Path) -> BarnetOpenDataAssessmentV1:
    """Load and strictly validate one committed Open Barnet assessment."""
    return BarnetOpenDataAssessmentV1.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _catalog_search_text(dataset: _CatalogDataset) -> str:
    resource_text = " ".join(
        f"{resource.title} {resource.filename} {resource.description}"
        for resource in dataset.resources
    )
    return f"{dataset.title} {dataset.description} {resource_text}".casefold()


def _require(*, condition: bool, code: str) -> None:
    if not condition:
        raise BarnetOpenDataEvidenceError(code)


__all__ = [
    "BarnetOpenDataAssessmentV1",
    "BarnetOpenDataEvidenceError",
    "derive_barnet_open_data_assessment",
    "load_barnet_open_data_assessment",
    "verify_barnet_open_data_assessment",
]
