# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004

"""Camden GeneralSearch discovery and checkpoint behavior."""

from datetime import date

import pytest
from pydantic import ValidationError

from yimby.authorities.camden import discovery
from yimby.domain import DiscoveryWindow


def _window(*, include_open: bool = True) -> DiscoveryWindow:
    return DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=include_open,
    )


def test_camden_query_inventory_is_typed_ordered_and_scope_owned() -> None:
    scope = discovery.CamdenDiscoveryScopeV1.from_window(_window())

    inventory = discovery.camden_query_inventory(scope)

    assert [query.key for query in inventory] == [
        "date:DATE_RECEIVED:2026-08-18:2026-09-16",
        "date:DATE_VALID:2026-08-18:2026-09-16",
        "date:DATE_DECISION:2026-08-18:2026-09-16",
        "status:4:REGISTERED",
        "status:14:APPEAL LODGED",
    ]
    assert [query.model_dump(mode="json") for query in inventory[:3]] == [
        {
            "kind": "date",
            "field": field,
            "start": "2026-08-18",
            "end": "2026-09-16",
        }
        for field in ("DATE_RECEIVED", "DATE_VALID", "DATE_DECISION")
    ]
    assert [query.model_dump(mode="json") for query in inventory[3:]] == [
        {"kind": "status", "status": "4"},
        {"kind": "status", "status": "14"},
    ]
    closed_scope = discovery.CamdenDiscoveryScopeV1.from_window(
        _window(include_open=False)
    )
    assert len(discovery.camden_query_inventory(closed_scope)) == 3


def test_camden_checkpoint_migrates_only_the_exact_fixture_shape() -> None:
    checkpoint = discovery.CamdenCheckpointV1.model_validate_json(
        '{"view_state_page":"next"}'
    )

    assert isinstance(checkpoint.root, discovery.CamdenFixtureCheckpointV1)
    assert checkpoint.root.view_state_page == "next"
    with pytest.raises(ValidationError):
        discovery.CamdenCheckpointV1.model_validate(
            {"view_state_page": "next", "XMLLoc": "stale"}
        )


def test_camden_live_checkpoint_rejects_incoherent_progress() -> None:
    checkpoint = discovery.initial_live_checkpoint(_window())
    live = checkpoint.root
    assert isinstance(live, discovery.CamdenLiveCheckpointV1)
    assert isinstance(live.progress, discovery.CamdenBetweenQueriesV1)
    assert live.progress.next_query_index == 0
    assert live.query_inventory == discovery.camden_query_inventory(live.scope)

    raw = live.model_dump(mode="json")
    raw["progress"] = {"kind": "terminal"}
    with pytest.raises(ValidationError, match="terminal checkpoint"):
        discovery.CamdenCheckpointV1.model_validate(raw)

    raw = live.model_dump(mode="json")
    raw["XMLLoc"] = "stale-session-token"
    with pytest.raises(ValidationError, match="extra"):
        discovery.CamdenCheckpointV1.model_validate(raw)


def test_camden_live_checkpoint_requires_full_active_prefix_union() -> None:
    initial = discovery.initial_live_checkpoint(_window()).root
    assert isinstance(initial, discovery.CamdenLiveCheckpointV1)
    first = discovery.CamdenSeenReferenceV1(reference="2026/1/P", locator="1")
    second = discovery.CamdenSeenReferenceV1(reference="2026/2/P", locator="2")
    raw = initial.model_dump(mode="json")
    raw["seen_references"] = [first.model_dump(mode="json")]
    raw["progress"] = {
        "kind": "paging-query",
        "query_index": 0,
        "reported_count": 20,
        "next_offset": 10,
        "ordered_prefix": [
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        ],
    }
    with pytest.raises(ValidationError, match="next offset"):
        discovery.CamdenCheckpointV1.model_validate(raw)

    raw["progress"]["ordered_prefix"] = [first.model_dump(mode="json")] * 10
    with pytest.raises(ValidationError, match="unique references"):
        discovery.CamdenCheckpointV1.model_validate(raw)
