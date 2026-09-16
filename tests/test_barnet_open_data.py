# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D100, D103, PLR2004

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from yimby.authorities.barnet.open_data import (
    BarnetOpenDataAssessmentV1,
    BarnetOpenDataEvidenceError,
    derive_barnet_open_data_assessment,
    load_barnet_open_data_assessment,
    verify_barnet_open_data_assessment,
)


def _metadata_payloads() -> tuple[bytes, bytes]:
    resources: dict[str, dict[str, Any]] = {}
    catalog_resources: list[dict[str, Any]] = []
    for index in range(13):
        resource_id = f"r{index:02d}"
        title = f"Planning Applications Decided 20{index:02d}/20{index + 1:02d}"
        timeframe_from = f"20{index:02d}-04"
        timeframe_to = f"20{index + 1:02d}-03"
        if index == 0:
            resource_id = "cp9"
            timeframe_from = "1965-05"
            timeframe_to = "2009-03"
        if index == 12:
            resource_id = "xl9"
            title = "Planning Applications Decided 2020/2021"
            timeframe_from = "2020-04"
            timeframe_to = "2021-03"
        resources[resource_id] = {
            "title": title,
            "format": "csv",
            "timeframe": {"from": timeframe_from, "to": timeframe_to},
        }
        catalog_resources.append(
            {
                "id": resource_id,
                "title": title,
                "filename": f"{resource_id}.csv",
                "description": "",
                "timeframeFrom": timeframe_from,
                "timeframeTo": timeframe_to,
            }
        )

    updated_at = "2021-02-01T16:28:29Z"
    dataset = {
        "id": "2nq32",
        "title": "Planning Applications",
        "updatedAt": updated_at,
        "resources": resources,
        "links": {},
        "contact": {"name": "omitted by derived artifact"},
    }
    catalog: list[dict[str, Any]] = [
        {
            "id": "2nq32",
            "title": "Planning Applications",
            "description": "Current and historic planning applications",
            "updatedAt": updated_at,
            "resources": catalog_resources,
            "links": [],
            "contact": {"name": "omitted by derived artifact"},
        },
        {
            "id": "e1g8k",
            "title": (
                "Planning Applications Documents_POC (Internal use only_dummy data)"
            ),
            "description": "Dummy document proof of concept",
            "updatedAt": "2025-03-21T12:03:17.818Z",
            "resources": [
                {
                    "id": f"p{index}",
                    "title": f"Dummy document {index}.pdf",
                    "filename": f"dummy-{index}.pdf",
                    "description": "",
                    "timeframeFrom": None,
                    "timeframeTo": None,
                }
                for index in range(6)
            ],
            "links": [],
        },
    ]
    catalog.extend(
        {
            "id": f"safe-{index}",
            "title": f"Unrelated public dataset {index}",
            "description": "Unrelated metadata",
            "updatedAt": "2026-09-01T00:00:00Z",
            "resources": [],
            "links": [],
        }
        for index in range(395)
    )
    return _json_bytes(dataset), _json_bytes(catalog)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _dataset_object(payload: bytes) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(payload))


def _catalog_object(payload: bytes) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", json.loads(payload))


def test_committed_barnet_open_data_assessment_is_strict_and_historical() -> None:
    path = (
        Path(__file__).parents[1]
        / "docs"
        / "evidence"
        / "barnet-open-data-assessment-2026-09-16.json"
    )
    artifact_text = path.read_text(encoding="utf-8")
    artifact = load_barnet_open_data_assessment(path)

    assert artifact.planning_dataset.resource_count == 13
    assert artifact.planning_dataset.newest_timeframe_to == "2021-03"
    assert artifact.catalog.matching_dataset_ids == ("2nq32", "e1g8k")
    assert artifact.catalog.newer_nonqualifying_dataset.timeframe_present is False
    assert artifact.coverage.decision == "historical-fallback-insufficient"
    assert artifact.coverage.preserved_live_blocker == "official-http-429"
    assert all(
        marker not in artifact_text.casefold()
        for marker in ("keyval", "csrf", "cookie", "<html")
    )

    payload = artifact.model_dump()
    with pytest.raises(ValidationError):
        BarnetOpenDataAssessmentV1.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        BarnetOpenDataAssessmentV1.model_validate(
            {
                **payload,
                "sources": {
                    **artifact.sources.model_dump(),
                    "catalog_payload_sha256": "not-a-digest",
                },
            }
        )
    with pytest.raises(ValidationError):
        BarnetOpenDataAssessmentV1.model_validate(
            {
                **payload,
                "coverage": {
                    **artifact.coverage.model_dump(),
                    "current_30_day_population": True,
                },
            }
        )


def test_barnet_open_data_assessment_is_derived_from_retained_metadata() -> None:
    dataset_payload, catalog_payload = _metadata_payloads()
    assessment = derive_barnet_open_data_assessment(
        dataset_payload,
        catalog_payload,
        observed_at=datetime(2026, 9, 16, 15, 22, 36, tzinfo=UTC),
    )

    assert assessment.planning_dataset.resource_count == 13
    assert assessment.catalog.public_dataset_count == 397
    assert assessment.catalog.matching_dataset_ids == ("2nq32", "e1g8k")
    assert "omitted by derived artifact" not in assessment.model_dump_json()
    verify_barnet_open_data_assessment(
        assessment,
        dataset_payload,
        catalog_payload,
    )

    changed_sources = assessment.sources.model_copy(
        update={"catalog_payload_sha256": "0" * 64}
    )
    changed_assessment = assessment.model_copy(update={"sources": changed_sources})
    with pytest.raises(BarnetOpenDataEvidenceError, match="assessment-mismatch"):
        verify_barnet_open_data_assessment(
            changed_assessment,
            dataset_payload,
            catalog_payload,
        )


def test_barnet_open_data_derivation_rejects_unbound_or_ineligible_metadata() -> None:
    dataset_payload, catalog_payload = _metadata_payloads()
    observed_at = datetime(2026, 9, 16, 15, 22, 36, tzinfo=UTC)

    mismatched_catalog = _catalog_object(catalog_payload)
    mismatched_catalog[0]["resources"].pop()
    with pytest.raises(
        BarnetOpenDataEvidenceError, match="dataset-catalog-resource-mismatch"
    ):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(mismatched_catalog),
            observed_at=observed_at,
        )

    mismatched_update = _catalog_object(catalog_payload)
    mismatched_update[0]["updatedAt"] = "2021-02-02T00:00:00Z"
    with pytest.raises(BarnetOpenDataEvidenceError, match="dataset-update-mismatch"):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(mismatched_update),
            observed_at=observed_at,
        )

    mixed_format = _dataset_object(dataset_payload)
    mixed_format["resources"]["cp9"]["format"] = "txt"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            _json_bytes(mixed_format),
            catalog_payload,
            observed_at=observed_at,
        )

    unclassified = _dataset_object(dataset_payload)
    unclassified["resources"]["cp9"]["title"] = "Unclassified extract"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            _json_bytes(unclassified),
            catalog_payload,
            observed_at=observed_at,
        )

    extra_match = _catalog_object(catalog_payload)
    extra_match[2]["description"] = "A successor planning application feed"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(extra_match),
            observed_at=observed_at,
        )

    mixed_poc_format = _catalog_object(catalog_payload)
    mixed_poc_format[1]["resources"][0]["filename"] = "dummy.txt"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(mixed_poc_format),
            observed_at=observed_at,
        )

    timed_poc = _catalog_object(catalog_payload)
    timed_poc[1]["resources"][0]["timeframeFrom"] = "2025-01"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(timed_poc),
            observed_at=observed_at,
        )

    end_timed_poc = _catalog_object(catalog_payload)
    end_timed_poc[1]["resources"][0]["timeframeTo"] = "2025-01"
    with pytest.raises(ValidationError):
        derive_barnet_open_data_assessment(
            dataset_payload,
            _json_bytes(end_timed_poc),
            observed_at=observed_at,
        )
