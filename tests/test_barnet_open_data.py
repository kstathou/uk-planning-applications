# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D100, D103, PLR2004

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from yimby.authorities.barnet.open_data import (
    BarnetOpenDataAssessmentV1,
    load_barnet_open_data_assessment,
)


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
