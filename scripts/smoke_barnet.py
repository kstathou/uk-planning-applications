# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Run one explicitly authorised, bounded Barnet live smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.domain import (
    DiscoveryWindow,
    FrozenModel,
    SourceReference,
    StoredCheckpoint,
)
from yimby.http_transport import HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import Sequence


class BarnetSmokeState(FrozenModel):
    """Non-secret checkpoint state for a resumable bounded smoke."""

    week: date
    checkpoint: StoredCheckpoint | None = None
    first_reference: SourceReference | None = None
    discovered: int = 0


class SmokeConfigurationError(ValueError):
    """The operator did not supply a bounded, explicit live request."""

    def __init__(self) -> None:
        """Require a Monday because Barnet weekly lists are Monday-based."""
        super().__init__("--week must be a Monday")


class SmokeAttachmentPolicyError(RuntimeError):
    """The smoke observed an attachment body request."""

    def __init__(self) -> None:
        """Report the invariant without disclosing response details."""
        super().__init__("attachment body request observed")


class SmokeStateWeekError(ValueError):
    """A checkpoint belongs to a different weekly query."""

    def __init__(self, expected: date, actual: date) -> None:
        """Prevent state from crossing a weekly search boundary."""
        super().__init__(f"smoke state is for {actual}, not requested week {expected}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="explicitly permit live requests to the Barnet public register",
    )
    parser.add_argument(
        "--week",
        required=True,
        metavar="YYYY-MM-DD",
        help="recorded Monday whose seven-day weekly list will be queried",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="non-secret checkpoint file; defaults below .yimby",
    )
    return parser


def _load_state(path: Path, week: date) -> BarnetSmokeState:
    if not path.exists():
        return BarnetSmokeState(week=week)
    state = BarnetSmokeState.model_validate_json(path.read_text())
    if state.week != week:
        raise SmokeStateWeekError(week, state.week)
    return state


def _save_state(path: Path, state: BarnetSmokeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(state.model_dump_json())
    temporary.replace(path)


async def _smoke(week: date, state_path: Path) -> dict[str, object]:
    if week.weekday() != 0:
        raise SmokeConfigurationError
    session = HttpxPortalSession()
    state = _load_state(state_path, week)
    complete = False
    try:
        window = DiscoveryWindow(
            start=week,
            end=week + timedelta(days=6),
            include_open=False,
        )
        async for batch in BARNET_PACKAGE.discover(
            session,
            window,
            state.checkpoint,
        ):
            complete = batch.complete
            first_reference = state.first_reference
            if first_reference is None and batch.references:
                first_reference = batch.references[0]
            state = state.model_copy(
                update={
                    "checkpoint": batch.next_checkpoint,
                    "first_reference": first_reference,
                    "discovered": state.discovered + len(batch.references),
                }
            )
            _save_state(state_path, state)
        observation = (
            None
            if state.first_reference is None
            else await BARNET_PACKAGE.collect(session, state.first_reference)
        )
        if session.attachment_body_requests != 0:
            raise SmokeAttachmentPolicyError
        return {
            "authority": "barnet",
            "week": week.isoformat(),
            "complete": complete,
            "discovered": state.discovered,
            "fetched": 0 if observation is None else 1,
            "reference": (
                None
                if observation is None
                else observation.normalised.reference.reference
            ),
            "requests": len(session.requested_urls),
            "transferred_bytes": session.transferred_bytes,
            "attachment_body_requests": session.attachment_body_requests,
            "state": str(state_path),
        }
    finally:
        await session.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Validate opt-in arguments and print one structured smoke result."""
    arguments = _parser().parse_args(argv)
    if not arguments.confirm_live:
        result: dict[str, object] = {
            "ok": False,
            "error": "pass --confirm-live to permit live Barnet requests",
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    state_path: Path | None = arguments.state
    try:
        week = date.fromisoformat(arguments.week)
        if state_path is None:
            state_path = Path(".yimby") / f"smoke-barnet-{week.isoformat()}.json"
        result = {"ok": True, **asyncio.run(_smoke(week, state_path))}
    except (ValueError, RuntimeError) as error:
        result = {
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
        if state_path is not None:
            result["state"] = str(state_path)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
