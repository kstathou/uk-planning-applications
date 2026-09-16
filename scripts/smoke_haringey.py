# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Probe one Haringey seven-day quick-link page without opening files."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import TYPE_CHECKING

from yimby.authorities.haringey.page_object import HaringeyPlaywrightSession

if TYPE_CHECKING:
    from collections.abc import Sequence


async def _smoke() -> dict[str, object]:
    session = await HaringeyPlaywrightSession.create()
    try:
        page = await session.validated_last_seven_days(1)
        return {
            "authority": "haringey",
            "complete": page.reported_page_count == 1,
            "visible_references": len(page.hits),
            "reported_results": page.reported_result_count,
            "reported_pages": page.reported_page_count,
            "requests": len(session.requested_urls),
            "transferred_bytes": session.transferred_bytes,
            "attachment_body_requests": session.attachment_body_requests,
        }
    finally:
        await session.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Require explicit opt-in before launching the browser page object."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.confirm_live:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "pass --confirm-live to permit live haringey requests",
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
