# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Probe one bounded Cheshire East valid-date-from result table."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from yimby.authorities.cheshire_east.adapter import CheshireEastAdapter
from yimby.domain import DiscoveryWindow
from yimby.http_transport import HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import Sequence


async def _smoke() -> dict[str, object]:
    today = datetime.now(UTC).date()
    session = HttpxPortalSession()
    discovery = CheshireEastAdapter().discover(
        session,
        DiscoveryWindow(start=today, end=today, include_open=False),
        None,
    )
    try:
        batch = await anext(discovery)
        return {
            "authority": "cheshire-east",
            "complete": batch.complete,
            "visible_references": len(batch.references),
            "requests": len(session.requested_urls),
            "transferred_bytes": session.transferred_bytes,
            "attachment_body_requests": session.attachment_body_requests,
        }
    finally:
        await discovery.aclose()
        await session.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Require explicit opt-in before the two-request discovery probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.confirm_live:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "pass --confirm-live to permit live cheshire-east requests"
                    ),
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        result = {"ok": True, **asyncio.run(_smoke())}
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
