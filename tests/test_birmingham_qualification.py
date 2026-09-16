# Copyright (c) 2026 Kostas Stathoulopoulos

"""Fail-closed qualification contract for Birmingham's ArcGIS layer."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

import yimby.authorities.birmingham.adapter as birmingham
from yimby.domain import (
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceReference,
    TransportMode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType
    from typing import Any

    from yimby.transport import PortalRequest

_LAYER_URL = (
    "https://maps.birmingham.gov.uk/server/rest/services/mybrummap/"
    "mybrummap_Planning_OGCServices/MapServer/12"
)
_RECEIPT_NAME = "birmingham-qualification-v1.json"
_CONFIG_ERROR = 2
_RECENT_WHERE = (
    "Received >= DATE '2026-08-18 00:00:00' AND Received < DATE '2026-09-17 00:00:00'"
)
_YEAR_2025_WHERE = (
    "Received >= DATE '2025-01-01 00:00:00' AND Received < DATE '2026-01-01 00:00:00'"
)
_JULY_2026_WHERE = (
    "Received >= DATE '2026-07-01 00:00:00' AND Received < DATE '2026-08-01 00:00:00'"
)
_AUGUST_2026_WHERE = (
    "Received >= DATE '2026-08-01 00:00:00' AND Received < DATE '2026-09-01 00:00:00'"
)
_UNRESOLVED_WHERE = (
    "APPLICATION_DECISION IS NULL AND Decision_Date IS NULL AND Date_Issued IS NULL"
)
_DECISION_DATE_MISMATCH_WHERE = (
    "(APPLICATION_DECISION IS NULL AND Decision_Date IS NOT NULL) OR "
    "(APPLICATION_DECISION IS NOT NULL AND Decision_Date IS NULL)"
)
_QUERY_NAMES = (
    "layer-metadata",
    "layer-profile",
    "count-2025",
    "count-2026-july",
    "count-2026-august",
    "recent-count",
    "recent-page-0",
    "recent-page-25",
    "recent-page-50",
    "application-decision-groups",
    "decision-date-mismatch-count",
    "oldest-null-decision-sample",
    "unresolved-candidate-profile",
    "unresolved-oldest-sample",
)


class _NeverFetchSession:
    def __init__(self, mode: TransportMode) -> None:
        self.mode = mode
        self.fetch_calls = 0

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.fetch_calls += 1
        message = f"unexpected fetch: {request.url}"
        raise AssertionError(message)


@pytest.mark.parametrize(
    "mode",
    [TransportMode.LIVE, TransportMode.BROWSER, TransportMode.NOT_RUN],
)
def test_birmingham_nonfixture_discover_rejects_before_fetch(
    mode: TransportMode,
) -> None:
    """Never derive a live search route from the old synthetic fixture shape."""
    session = _NeverFetchSession(mode)
    adapter = birmingham.BirminghamAdapter()
    error_type = birmingham.BirminghamLiveContractUnavailableError

    async def first_batch() -> None:
        batches = adapter.discover(
            session,
            DiscoveryWindow(start=date(2026, 8, 18), end=date(2026, 9, 16)),
            None,
        )
        await anext(batches)

    with pytest.raises(error_type):
        asyncio.run(first_batch())
    assert session.fetch_calls == 0


@pytest.mark.parametrize(
    "mode",
    [TransportMode.LIVE, TransportMode.BROWSER, TransportMode.NOT_RUN],
)
def test_birmingham_nonfixture_fetch_rejects_before_fetch(
    mode: TransportMode,
) -> None:
    """Never derive a live detail route from the old synthetic fixture shape."""
    session = _NeverFetchSession(mode)
    adapter = birmingham.BirminghamAdapter()
    error_type = birmingham.BirminghamLiveContractUnavailableError
    reference = SourceReference(
        source_id=birmingham.SOURCE,
        reference="2026/05750/PA",
    )

    with pytest.raises(error_type):
        asyncio.run(adapter.fetch(session, reference))
    assert session.fetch_calls == 0


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_birmingham.py"
    name = "_test_qualify_birmingham"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _arcgis_bodies() -> tuple[bytes, ...]:
    fields = (
        "OBJECTID",
        "REFERENCE",
        "TYPE",
        "Received",
        "LOCATION",
        "Dev",
        "Date_Accepted",
        "APPLICATION_DECISION",
        "Decision_Date",
        "Date_Issued",
        "APPEAL_DECISION",
        "Appeal_Decision_Date",
        "Officer",
        "PGP_PK",
        "PA_NO",
    )
    metadata = {
        "id": 12,
        "name": "Post 1990 Planning Application",
        "type": "Feature Layer",
        "maxRecordCount": 1000,
        "hasAttachments": False,
        "relationships": [],
        "fields": [{"name": field, "type": "esriFieldTypeString"} for field in fields],
        "advancedQueryCapabilities": {
            "supportsPagination": True,
            "supportsStatistics": True,
            "supportsOrderBy": True,
            "supportsDistinct": True,
        },
    }
    recent_features = [
        {
            "attributes": {
                "OBJECTID": offset,
                "REFERENCE": f"2026/{5600 + offset:05d}/PA",
                "Received": 1_789_084_800_000 + offset,
            }
        }
        for offset in range(1, 70)
    ]
    recent_features.append(
        {
            "attributes": {
                "OBJECTID": 70,
                "REFERENCE": "2026/05750/PA",
                "Received": 1_789_430_400_000,
            }
        }
    )
    return (
        _json_bytes(metadata),
        _json_bytes(
            {
                "features": [
                    {
                        "attributes": {
                            "record_count": 254_064,
                            "max_objectid": 254_064,
                            "latest_received": 1_789_430_400_000,
                            "latest_accepted": 1_793_059_200_000,
                        }
                    }
                ]
            }
        ),
        _json_bytes({"count": 5802}),
        _json_bytes({"count": 453}),
        _json_bytes({"count": 121}),
        _json_bytes({"count": 70}),
        _json_bytes({"features": recent_features[:25], "exceededTransferLimit": True}),
        _json_bytes(
            {"features": recent_features[25:50], "exceededTransferLimit": True}
        ),
        _json_bytes({"features": recent_features[50:], "exceededTransferLimit": False}),
        _json_bytes(
            {
                "features": [
                    {
                        "attributes": {
                            "APPLICATION_DECISION": None,
                            "record_count": 3276,
                            "earliest_received": -2_177_452_800_000,
                            "latest_received": 1_789_430_400_000,
                        }
                    },
                    {
                        "attributes": {
                            "APPLICATION_DECISION": "Approve",
                            "record_count": 200_000,
                        }
                    },
                ]
            }
        ),
        _json_bytes({"count": 0}),
        _json_bytes(
            {
                "features": [
                    {
                        "attributes": {
                            "REFERENCE": "2000/24139/PA",
                            "TYPE": "Post Decision Amendment",
                            "Received": -2_177_452_800_000,
                            "APPLICATION_DECISION": None,
                            "Decision_Date": None,
                            "Date_Issued": 995_328_000_000,
                            "APPEAL_DECISION": None,
                        }
                    }
                ],
                "exceededTransferLimit": False,
            }
        ),
        _json_bytes(
            {
                "features": [
                    {
                        "attributes": {
                            "record_count": 1419,
                            "earliest_received": 1_243_555_200_000,
                            "latest_received": 1_789_430_400_000,
                        }
                    }
                ]
            }
        ),
        _json_bytes(
            {
                "features": [
                    {
                        "attributes": {
                            "REFERENCE": "2009/02998/PA",
                            "TYPE": "Enforcement",
                            "Received": 1_243_555_200_000,
                            "APPLICATION_DECISION": None,
                            "Decision_Date": None,
                            "Date_Issued": None,
                            "APPEAL_DECISION": "Dismissed",
                        }
                    },
                    {
                        "attributes": {
                            "REFERENCE": "2009/04150/PA",
                            "TYPE": "Master Plan",
                            "Received": 1_249_084_800_000,
                            "APPLICATION_DECISION": None,
                            "Decision_Date": None,
                            "Date_Issued": None,
                            "APPEAL_DECISION": "Withdrawn",
                        }
                    },
                ],
                "exceededTransferLimit": False,
            }
        ),
    )


class _ArcgisSession:
    def __init__(self, bodies: Sequence[bytes]) -> None:
        self._bodies = tuple(bodies)
        self.requests: list[PortalRequest] = []
        self.closed = False

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        index = len(self.requests)
        self.requests.append(request)
        body = self._bodies[index]
        return EvidenceCapture(
            url=request.url,
            media_type="application/json",
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(str(request.url) for request in self.requests)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return sum(len(body) for body in self._bodies[: len(self.requests)])

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.LIVE

    async def aclose(self) -> None:
        self.closed = True


def _args(data_dir: Path) -> list[str]:
    return [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]


@pytest.mark.parametrize(
    ("remove", "replacement", "error"),
    [
        ("--confirm-live", None, "confirmation-required"),
        ("--include-open", None, "include-open-required"),
        ("2026-08-18", "2026-08-19", "exact-30-day-window-required"),
        ("2026-09-16", "2026-09-17", "exact-30-day-window-required"),
    ],
)
def test_birmingham_qualification_requires_exact_safe_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    remove: str,
    replacement: str | None,
    error: str,
) -> None:
    """Reject live access until every exact-scope safety option is explicit."""
    module = _qualification_module()
    created = 0

    def session_factory() -> _ArcgisSession:
        nonlocal created
        created += 1
        return _ArcgisSession(_arcgis_bodies())

    args = _args(tmp_path / "qualification")
    index = args.index(remove)
    if replacement is None:
        args.pop(index)
    else:
        args[index] = replacement

    assert module.main(args, session_factory=session_factory) == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == error
    assert created == 0


def test_birmingham_qualification_rejects_nonempty_data_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never mix a blocked qualification with caller-owned durable state."""
    module = _qualification_module()
    data_dir = tmp_path / "occupied"
    data_dir.mkdir()
    sentinel = data_dir / "preserve"
    sentinel.write_text("caller-owned", encoding="utf-8")

    def session_factory() -> _ArcgisSession:
        message = "validation must happen before session construction"
        raise AssertionError(message)

    result = module.main(_args(data_dir), session_factory=session_factory)
    assert result == _CONFIG_ERROR
    assert json.loads(capsys.readouterr().err)["error"] == "data-dir-not-empty"
    assert sentinel.read_text(encoding="utf-8") == "caller-owned"


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _assert_query_inventory(
    receipt: dict[str, Any],
    bodies: tuple[bytes, ...],
    data_dir: Path,
    session: _ArcgisSession,
) -> None:
    inventory = receipt["query_inventory"]
    assert [item["name"] for item in inventory] == list(_QUERY_NAMES)
    assert [item["url"] for item in inventory] == list(session.requested_urls)
    assert len(inventory) == len(bodies)
    for item, expected_body in zip(inventory, bodies, strict=True):
        evidence = item["evidence"]
        stored = data_dir / "evidence" / evidence["stored_path"]
        retained = gzip.decompress(stored.read_bytes())
        assert retained == expected_body
        assert evidence == {
            "sha256": sha256(expected_body).hexdigest(),
            "stored_path": evidence["stored_path"],
            "media_type": "application/json",
            "byte_count": len(expected_body),
        }

    urls = dict(zip(_QUERY_NAMES, session.requested_urls, strict=True))
    assert urls["layer-metadata"] == f"{_LAYER_URL}?f=json"
    expected_count_queries = {
        "count-2025": _YEAR_2025_WHERE,
        "count-2026-july": _JULY_2026_WHERE,
        "count-2026-august": _AUGUST_2026_WHERE,
    }
    for name, where in expected_count_queries.items():
        assert _query(urls[name]) == {
            "f": ["json"],
            "where": [where],
            "returnCountOnly": ["true"],
        }
    assert _query(urls["recent-count"]) == {
        "f": ["json"],
        "where": [_RECENT_WHERE],
        "returnCountOnly": ["true"],
    }
    assert _query(urls["recent-page-0"]) == {
        "f": ["json"],
        "where": [_RECENT_WHERE],
        "outFields": ["OBJECTID,REFERENCE,Received"],
        "orderByFields": ["OBJECTID ASC"],
        "resultOffset": ["0"],
        "resultRecordCount": ["25"],
        "returnGeometry": ["false"],
    }
    assert _query(urls["recent-page-25"])["resultOffset"] == ["25"]
    assert _query(urls["recent-page-50"])["resultOffset"] == ["50"]
    decision_groups = _query(urls["application-decision-groups"])
    assert decision_groups["groupByFieldsForStatistics"] == ["APPLICATION_DECISION"]
    assert _query(urls["decision-date-mismatch-count"])["where"] == [
        _DECISION_DATE_MISMATCH_WHERE
    ]
    assert _query(urls["unresolved-candidate-profile"])["where"] == [_UNRESOLVED_WHERE]
    assert _query(urls["unresolved-oldest-sample"])["where"] == [_UNRESOLVED_WHERE]


def test_birmingham_qualification_persists_typed_blocked_receipt(
    tmp_path: Path,
) -> None:
    """Persist the proven ArcGIS subset without claiming complete collection."""
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    data_dir.mkdir()
    bodies = _arcgis_bodies()
    sessions: list[_ArcgisSession] = []

    def session_factory() -> _ArcgisSession:
        session = _ArcgisSession(bodies)
        sessions.append(session)
        return session

    result = module.main(
        _args(data_dir),
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 1
    assert len(sessions) == 1
    assert sessions[0].closed is True
    receipt_path = data_dir / _RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    typed = module.BirminghamBlockedQualificationReceiptV1.model_validate(receipt)
    assert typed.model_dump(mode="json") == receipt
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "birmingham"
    assert receipt["outcome"] == "blocked"
    assert receipt["created_at"] == "2026-09-16T12:00:00Z"
    assert receipt["scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
        "inclusive_days": 30,
    }
    assert receipt["source_freshness"] == {
        "status": "not-proven",
        "latest_received": "2026-09-15",
        "latest_record_is_current": True,
        "volume_continuity": "not-proven",
        "calendar_2025_count": 5802,
        "july_2026_count": 453,
        "august_2026_count": 121,
        "rolling_30_day_count": 70,
        "reason": "unexplained-recent-volume-decline",
    }
    assert receipt["recent_discovery"] == {
        "status": "proven",
        "reported_count": 70,
        "page_feature_count": 70,
        "unique_reference_count": 70,
        "unique_objectid_count": 70,
        "count_page_reference_agreement": True,
        "page_count": 3,
        "terminal_page_observed": True,
        "latest_reference": "2026/05750/PA",
        "latest_received": "2026-09-15",
    }
    assert receipt["older_open"] == {
        "status": "not-proven",
        "reason": "no-explicit-case-status-field",
        "application_decision_null_count": 3276,
        "decision_date_mismatch_count": 0,
        "unresolved_candidate_count": 1419,
        "contradictions": [
            "issued-without-application-decision",
            "appeal-completed-without-decision-or-issued",
            "non-planning-register-record",
        ],
    }
    assert receipt["active_appeals"] == {
        "status": "not-proven",
        "reason": "no-appeal-lodged-or-current-status-field",
    }
    assert receipt["sections"] == {
        "documents": {"status": "not-exposed", "item_count": 0},
        "comments": {"status": "not-exposed", "item_count": 0},
        "conditions": {"status": "not-exposed", "item_count": 0},
        "consultations": {"status": "not-exposed", "item_count": 0},
        "relationships": {"status": "not-exposed", "item_count": 0},
    }
    assert receipt["transport"] == {
        "request_count": len(bodies),
        "attachment_body_requests": 0,
    }
    assert receipt["local_integrity"] == {
        "sqlite_integrity": "ok",
        "evidence_integrity": "verified",
        "evidence_count": len(bodies),
    }
    assert receipt["offline_replay"] == {
        "request_count": 0,
        "sqlite_integrity": "ok",
        "evidence_integrity": "verified",
    }
    assert receipt["registry_readiness"] == {
        "before": "blocked",
        "after": "blocked",
        "promotion_attempted": False,
    }
    assert receipt["checkpoint"] == {
        "schema_version": 1,
        "page_size": 25,
        "next_offset": 75,
        "terminal": True,
        "reported_count": 70,
        "collected_count": 70,
    }
    assert receipt["weekly_cycles"] == [
        {
            "ordinal": 1,
            "required_offset_days": 7,
            "relative_to": "successful-live-bootstrap",
            "status": "pending",
            "scheduled_at": None,
            "completed_at": None,
        },
        {
            "ordinal": 2,
            "required_offset_days": 14,
            "relative_to": "successful-live-bootstrap",
            "status": "pending",
            "scheduled_at": None,
            "completed_at": None,
        },
    ]

    _assert_query_inventory(receipt, bodies, data_dir, sessions[0])

    with sqlite3.connect(data_dir / "yimby.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    replay = module.replay_persisted_state(data_dir)
    assert replay.model_dump(mode="json") == receipt["offline_replay"]
    assert len(sessions) == 1


@pytest.mark.parametrize("failure", ["recent-total", "layer-schema"])
def test_birmingham_qualification_refuses_incoherent_arcgis_evidence(
    tmp_path: Path,
    failure: str,
) -> None:
    """Never write a receipt for an incoherent page or changed source schema."""
    module = _qualification_module()
    bodies = list(_arcgis_bodies())
    if failure == "recent-total":
        bodies[5] = _json_bytes({"count": 71})
    else:
        metadata = json.loads(bodies[0])
        metadata["type"] = "Map Layer"
        bodies[0] = _json_bytes(metadata)
    data_dir = tmp_path / failure
    session = _ArcgisSession(bodies)

    assert module.main(_args(data_dir), session_factory=lambda: session) == 1
    assert session.closed is True
    assert not (data_dir / _RECEIPT_NAME).exists()
