# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Run one exact-reference Camden live smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import TYPE_CHECKING

from yimby.adapters import AuthorityPackage
from yimby.authorities.camden.adapter import (
    CamdenAdapter,
    CamdenApplicationV1,
    CamdenCheckpointV1,
)
from yimby.http_transport import HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import Sequence


class SmokeAttachmentPolicyError(RuntimeError):
    """The smoke observed an attachment-body request."""

    def __init__(self) -> None:
        """Report the policy invariant without response content."""
        super().__init__("attachment body request observed")


async def _smoke(reference: str) -> dict[str, object]:
    adapter = CamdenAdapter()
    package = AuthorityPackage(adapter, CamdenApplicationV1, CamdenCheckpointV1)
    session = HttpxPortalSession()
    try:
        resolved = await adapter.resolve_exact(session, reference)
        observation = await package.collect(session, resolved)
        if session.attachment_body_requests:
            raise SmokeAttachmentPolicyError
        return {
            "authority": "camden",
            "reference": observation.normalised.reference.reference,
            "fetched": 1,
            "requests": len(session.requested_urls),
            "transferred_bytes": session.transferred_bytes,
            "attachment_body_requests": session.attachment_body_requests,
        }
    finally:
        await session.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Require opt-in before resolving one explicit Camden reference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--reference", default="2026/2706/L")
    arguments = parser.parse_args(argv)
    if not arguments.confirm_live:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "pass --confirm-live to permit live camden requests",
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        result = {"ok": True, **asyncio.run(_smoke(arguments.reference))}
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
