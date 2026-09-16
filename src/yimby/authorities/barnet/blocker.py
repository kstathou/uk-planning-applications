# Copyright (c) 2026 Kostas Stathoulopoulos

"""Sanitized, state-bound evidence for an incomplete Barnet bootstrap."""

from __future__ import annotations

import gzip
import sqlite3
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, Self, cast

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from yimby.authorities.barnet.adapter import BarnetCheckpointV1
from yimby.domain import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path

    from yimby.authorities.barnet.adapter import BarnetDiscoveryScope

_AUTHORITY_ID = "barnet"
_RECEIPT_NAME = "barnet-qualification-v1.json"
_RETAINED_FAILURE_CODES = ("SourceUnavailableError", "RateLimitedError")
_INCLUSIVE_WINDOW_SPAN_DAYS = 29
_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _StrictFrozenModel(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class BarnetBlockerScope(_StrictFrozenModel):
    """Non-identifying qualification scope safe to publish."""

    start: date
    end: date
    include_open: Literal[True]

    @model_validator(mode="after")
    def require_exact_window(self) -> Self:
        """Require the qualification command's exact inclusive 30-day scope."""
        if (self.end - self.start).days != _INCLUSIVE_WINDOW_SPAN_DAYS:
            _invalid("Barnet blocker scope must span exactly 30 days")
        return self


class BarnetBlockerCounts(_StrictFrozenModel):
    """Aggregate retained-state counts without application identities."""

    requests: int = Field(gt=0)
    discovered_references: int = Field(gt=0)
    persisted_applications: int = Field(ge=0)
    evidence_records: int = Field(gt=0)
    pending_retries: int = Field(gt=0)


class BarnetBlocker(_StrictFrozenModel):
    """The observed external blocker and its retained internal failure code."""

    code: Literal["official-http-429"] = "official-http-429"
    confirmation: Literal["read-only-official-page"] = "read-only-official-page"
    retained_failure_code: Literal["SourceUnavailableError", "RateLimitedError"]


class BarnetBlockerStateBinding(_StrictFrozenModel):
    """One-way hashes binding public aggregates to the private retained state."""

    checkpoint_schema_version: Literal[1] = 1
    checkpoint_payload_sha256: _SHA256
    evidence_digest_set_sha256: _SHA256


class PendingBarnetCycle(_StrictFrozenModel):
    """A required follow-up cycle not scheduled until bootstrap succeeds."""

    ordinal: Literal[1, 2]
    status: Literal["pending-bootstrap"] = "pending-bootstrap"


class BarnetBlockerSanitization(_StrictFrozenModel):
    """Explicit disclosure boundary for the committed blocker evidence."""

    omitted: tuple[
        Literal["application-identities"],
        Literal["session-material"],
        Literal["source-bodies"],
    ] = ("application-identities", "session-material", "source-bodies")


class BarnetQualificationBlockerV1(_StrictFrozenModel):
    """Publishable proof that Barnet qualification stopped fail-closed."""

    schema_version: Literal[1] = 1
    authority_id: Literal["barnet"] = "barnet"
    status: Literal["blocked"] = "blocked"
    observed_at: datetime
    scope: BarnetBlockerScope
    counts: BarnetBlockerCounts
    receipt_present: Literal[False]
    sqlite_integrity: Literal["ok"]
    blocker: BarnetBlocker
    state_binding: BarnetBlockerStateBinding
    later_cycles: tuple[PendingBarnetCycle, PendingBarnetCycle]
    sanitization: BarnetBlockerSanitization

    @model_validator(mode="after")
    def require_coherent_blocker(self) -> Self:
        """Reject aggregates or cycle claims inconsistent with a first bootstrap."""
        if self.observed_at.tzinfo is None:
            _invalid("Barnet blocker observation must be timezone-aware")
        if self.observed_at.date() != self.scope.end:
            _invalid("Barnet blocker observation must match the scope end")
        if self.counts.persisted_applications > self.counts.discovered_references:
            _invalid("persisted applications exceed discovered references")
        if tuple(cycle.ordinal for cycle in self.later_cycles) != (1, 2):
            _invalid("Barnet blocker requires both later cycles in order")
        return self


class BarnetBlockerEvidenceError(RuntimeError):
    """Retained qualification state cannot support a publishable blocker."""


def derive_barnet_blocker(
    data_dir: Path,
    *,
    official_http_429_confirmed: bool,
) -> BarnetQualificationBlockerV1:
    """Derive a sanitized blocker from one dedicated, retained Barnet target."""
    _require(
        condition=official_http_429_confirmed,
        code="official-http-429-confirmation-required",
    )
    database = data_dir / "yimby.sqlite3"
    _require(condition=database.is_file(), code="qualification-database-required")
    _require(
        condition=not (data_dir / _RECEIPT_NAME).exists(),
        code="qualification-receipt-must-be-absent",
    )

    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        integrity = tuple(
            row[0] for row in connection.execute("PRAGMA integrity_check")
        )
        _require(condition=integrity == ("ok",), code="sqlite-integrity-failed")
        authorities = tuple(
            row[0]
            for row in connection.execute(
                "SELECT authority_id FROM authorities ORDER BY authority_id"
            )
        )
        _require(
            condition=authorities == (_AUTHORITY_ID,),
            code="dedicated-barnet-target-required",
        )

        checkpoint_row = connection.execute(
            """
            SELECT schema_version, payload_json FROM checkpoints
            WHERE authority_id = ?
            """,
            (_AUTHORITY_ID,),
        ).fetchone()
        _require(
            condition=(
                checkpoint_row is not None and checkpoint_row["schema_version"] == 1
            ),
            code="barnet-checkpoint-required",
        )
        checkpoint_row = cast("sqlite3.Row", checkpoint_row)
        try:
            checkpoint = BarnetCheckpointV1.model_validate_json(
                checkpoint_row["payload_json"]
            )
        except ValueError as error:
            code = "barnet-checkpoint-invalid"
            raise BarnetBlockerEvidenceError(code) from error
        _require(
            condition=checkpoint.live_scope is not None,
            code="barnet-live-scope-required",
        )

        run_row = connection.execute(
            """
            SELECT detail.status, detail.finished_at, detail.request_count,
                   detail.failure_message
            FROM runs AS run
            JOIN run_details AS detail ON detail.run_id = run.id
            WHERE run.authority_id = ?
            ORDER BY run.rowid DESC
            LIMIT 1
            """,
            (_AUTHORITY_ID,),
        ).fetchone()
        _require(
            condition=(
                run_row is not None
                and run_row["status"] == "failed"
                and run_row["finished_at"] is not None
                and run_row["failure_message"] in _RETAINED_FAILURE_CODES
            ),
            code="rate-limited-failed-run-required",
        )
        run_row = cast("sqlite3.Row", run_row)

        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM discovery_queue
                 WHERE authority_id = :authority_id) AS discovered_references,
                (SELECT COUNT(*) FROM applications
                 WHERE authority_id = :authority_id) AS persisted_applications,
                (SELECT COUNT(*) FROM retry_queue
                 WHERE authority_id = :authority_id AND status = 'pending'
                ) AS pending_retries
            """,
            {"authority_id": _AUTHORITY_ID},
        ).fetchone()
        _require(condition=counts is not None, code="barnet-counts-required")
        counts = cast("sqlite3.Row", counts)

        evidence_rows = tuple(
            connection.execute("SELECT digest, path FROM evidence ORDER BY digest")
        )

    _verify_evidence(data_dir / "evidence", evidence_rows)
    checkpoint_payload = str(checkpoint_row["payload_json"])
    evidence_digest_payload = "\n".join(str(row["digest"]) for row in evidence_rows)
    scope = cast("BarnetDiscoveryScope", checkpoint.live_scope)
    return BarnetQualificationBlockerV1(
        observed_at=datetime.fromisoformat(run_row["finished_at"]),
        scope=BarnetBlockerScope(
            start=scope.start,
            end=scope.end,
            include_open=True,
        ),
        counts=BarnetBlockerCounts(
            requests=run_row["request_count"],
            discovered_references=counts["discovered_references"],
            persisted_applications=counts["persisted_applications"],
            evidence_records=len(evidence_rows),
            pending_retries=counts["pending_retries"],
        ),
        receipt_present=False,
        sqlite_integrity="ok",
        blocker=BarnetBlocker(retained_failure_code=run_row["failure_message"]),
        state_binding=BarnetBlockerStateBinding(
            checkpoint_payload_sha256=sha256(
                checkpoint_payload.encode("utf-8")
            ).hexdigest(),
            evidence_digest_set_sha256=sha256(
                evidence_digest_payload.encode("ascii")
            ).hexdigest(),
        ),
        later_cycles=(PendingBarnetCycle(ordinal=1), PendingBarnetCycle(ordinal=2)),
        sanitization=BarnetBlockerSanitization(),
    )


def load_barnet_blocker(path: Path) -> BarnetQualificationBlockerV1:
    """Load a committed Barnet blocker through its strict public schema."""
    return BarnetQualificationBlockerV1.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _verify_evidence(root: Path, rows: tuple[sqlite3.Row, ...]) -> None:
    resolved_root = root.resolve()
    for row in rows:
        candidate = (root / row["path"]).resolve()
        _require(
            condition=(candidate.is_relative_to(resolved_root) and candidate.is_file()),
            code="retained-evidence-path-invalid",
        )
        try:
            body = gzip.decompress(candidate.read_bytes())
        except (OSError, EOFError) as error:
            code = "retained-evidence-invalid"
            raise BarnetBlockerEvidenceError(code) from error
        _require(
            condition=sha256(body).hexdigest() == row["digest"],
            code="retained-evidence-digest-mismatch",
        )


def _require(*, condition: bool, code: str) -> None:
    if not condition:
        raise BarnetBlockerEvidenceError(code)


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)
