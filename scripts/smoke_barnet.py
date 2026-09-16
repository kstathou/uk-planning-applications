# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Run one explicitly authorised, bounded Barnet live smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING

from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.domain import DiscoveryWindow
from yimby.http_transport import HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yimby.domain import SourceReference


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
    return parser


async def _smoke(week: date) -> dict[str, object]:
    if week.weekday() != 0:
        raise SmokeConfigurationError
    session = HttpxPortalSession()
    references: list[SourceReference] = []
    checkpoint = None
    complete = False
    try:
        window = DiscoveryWindow(
            start=week,
            end=week + timedelta(days=6),
            include_open=False,
        )
        async for batch in BARNET_PACKAGE.discover(session, window, checkpoint):
            checkpoint = batch.next_checkpoint
            complete = batch.complete
            references.extend(batch.references)
        observation = (
            None
            if not references
            else await BARNET_PACKAGE.collect(session, references[0])
        )
        if session.attachment_body_requests != 0:
            raise SmokeAttachmentPolicyError
        return {
            "authority": "barnet",
            "week": week.isoformat(),
            "complete": complete,
            "discovered": len(references),
            "fetched": 0 if observation is None else 1,
            "reference": (
                None
                if observation is None
                else observation.normalised.reference.reference
            ),
            "requests": len(session.requested_urls),
            "transferred_bytes": session.transferred_bytes,
            "attachment_body_requests": session.attachment_body_requests,
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
    try:
        week = date.fromisoformat(arguments.week)
        result = {"ok": True, **asyncio.run(_smoke(week))}
    except (ValueError, RuntimeError) as error:
        result = {
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
