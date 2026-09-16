# Copyright (c) 2026 Kostas Stathoulopoulos

"""Deterministic research and reviewed public exports."""

from __future__ import annotations

import csv
import json
from enum import StrEnum
from typing import TYPE_CHECKING

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from yimby.domain import ApplicationView
    from yimby.store import SqliteStore


class ExportFormat(StrEnum):
    """Supported local export serialisations."""

    CSV = "csv"
    JSONL = "jsonl"
    PARQUET = "parquet"


class ExportProfile(StrEnum):
    """Reviewed export disclosure profiles."""

    PUBLIC = "public"
    RESEARCH = "research"


PUBLIC_ALLOWLIST = (
    "application_id",
    "authority_id",
    "reference",
    "proposal",
    "application_type",
    "status",
    "decision",
    "received_date",
    "validated_date",
    "decision_date",
    "address",
    "longitude",
    "latitude",
    "documents",
    "comment_count",
    "source_url",
    "source_reuse",
    "completeness",
)

RESEARCH_FIELDS = (
    *PUBLIC_ALLOWLIST,
    "aliases",
    "bng_easting",
    "bng_northing",
    "comments",
    "published_parties",
    "officer_name",
    "constraints",
    "conditions",
    "consultations",
    "events",
    "relationships",
    "normaliser_version",
    "observed_at",
)


def export_records(
    store: SqliteStore,
    destination: Path,
    output_format: ExportFormat,
    profile: ExportProfile,
) -> int:
    """Write current unsuppressed rows in stable application-ID order."""
    rows = [_row(view, profile) for view in store.application_views()]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if output_format == ExportFormat.JSONL:
        payload = "".join(
            f"{json.dumps(row, sort_keys=True, separators=(',', ':'))}\n"
            for row in rows
        )
        destination.write_text(payload)
    elif output_format == ExportFormat.CSV:
        fields = (
            PUBLIC_ALLOWLIST if profile == ExportProfile.PUBLIC else RESEARCH_FIELDS
        )
        with destination.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(
                {key: _csv_value(value) for key, value in row.items()} for row in rows
            )
    else:
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, destination, compression="zstd")
    return len(rows)


def _row(view: ApplicationView, profile: ExportProfile) -> dict[str, object]:
    application = view.application
    metadata = view.metadata
    location = metadata.location
    public: dict[str, object] = {
        "application_id": str(application.id),
        "authority_id": str(application.authority_id),
        "reference": application.reference,
        "proposal": application.proposal,
        "application_type": metadata.application_type,
        "status": application.status,
        "decision": metadata.decision,
        "received_date": _date(metadata.received_date),
        "validated_date": _date(metadata.validated_date),
        "decision_date": _date(metadata.decision_date),
        "address": metadata.address,
        "longitude": None if location is None else location.wgs84.longitude,
        "latitude": None if location is None else location.wgs84.latitude,
        "documents": [
            document.model_dump(mode="json") for document in application.documents
        ],
        "comment_count": len(application.comments),
        "source_url": None if metadata.source_url is None else str(metadata.source_url),
        "source_reuse": (
            "Verify the linked authority source's current reuse terms before "
            "republishing."
        ),
        "completeness": application.completeness.model_dump(mode="json"),
    }
    if profile == ExportProfile.PUBLIC:
        return public
    return {
        **public,
        "aliases": list(metadata.aliases),
        "bng_easting": None if location is None else location.bng_easting,
        "bng_northing": None if location is None else location.bng_northing,
        "comments": [
            comment.model_dump(mode="json") for comment in application.comments
        ],
        "published_parties": list(metadata.published_parties),
        "officer_name": metadata.officer_name,
        "constraints": list(metadata.constraints),
        "conditions": list(metadata.conditions),
        "consultations": list(metadata.consultations),
        "events": [event.model_dump(mode="json") for event in metadata.events],
        "relationships": [
            relationship.model_dump(mode="json")
            for relationship in metadata.relationships
        ],
        "normaliser_version": view.normaliser_version,
        "observed_at": view.observed_at.isoformat(),
    }


def _csv_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _date(value: date | None) -> str | None:
    if value is None:
        return None
    return str(value)
