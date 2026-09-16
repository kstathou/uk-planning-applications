# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Shared bounded runner for captured non-IDOX authority contracts."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date  # noqa: TC003 - Pydantic resolves this annotation at runtime.
from pathlib import Path
from typing import TYPE_CHECKING

from yimby.domain import DiscoveryWindow, FrozenModel, SourceReference, StoredCheckpoint
from yimby.http_transport import HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yimby.adapters import RunnableAuthority


class CapturedSmokeState(FrozenModel):
    """Non-secret checkpoint for one bounded authority interval."""

    authority: str
    start: date
    end: date
    checkpoint: StoredCheckpoint | None = None
    first_reference: SourceReference | None = None
    discovered: int = 0


class SmokeStateBoundaryError(ValueError):
    """Saved state belongs to another authority or interval."""

    def __init__(self) -> None:
        """Describe the safe persisted-state mismatch."""
        super().__init__("smoke state belongs to another boundary")


class SmokeAttachmentPolicyError(RuntimeError):
    """The smoke observed an attachment-body request."""

    def __init__(self) -> None:
        """Report the policy invariant without response content."""
        super().__init__("attachment body request observed")


def _parser(authority: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Run one explicitly authorised, bounded {authority} live smoke."
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--state", type=Path)
    return parser


def _load_state(
    path: Path, authority: str, start: date, end: date
) -> CapturedSmokeState:
    if not path.exists():
        return CapturedSmokeState(authority=authority, start=start, end=end)
    state = CapturedSmokeState.model_validate_json(path.read_text())
    if (state.authority, state.start, state.end) != (authority, start, end):
        raise SmokeStateBoundaryError
    return state


def _save_state(path: Path, state: CapturedSmokeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(state.model_dump_json())
    temporary.replace(path)


async def _smoke(
    authority: str,
    package: RunnableAuthority,
    start: date,
    end: date,
    state_path: Path,
) -> dict[str, object]:
    session = HttpxPortalSession()
    state = _load_state(state_path, authority, start, end)
    complete = False
    observation = None
    try:
        window = DiscoveryWindow(start=start, end=end, include_open=False)
        async for batch in package.discover(session, window, state.checkpoint):
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
        if state.first_reference is not None:
            observation = await package.collect(session, state.first_reference)
        if session.attachment_body_requests:
            raise SmokeAttachmentPolicyError
        return {
            "authority": authority,
            "start": start.isoformat(),
            "end": end.isoformat(),
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


def run_discovery_smoke(
    authority: str,
    package: RunnableAuthority,
    start: date,
    end: date,
    argv: Sequence[str] | None = None,
) -> int:
    """Run a captured interval only after explicit live confirmation."""
    arguments = _parser(authority).parse_args(argv)
    if not arguments.confirm_live:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"pass --confirm-live to permit live {authority} requests",
                },
                sort_keys=True,
            )
        )
        return 2
    state_path: Path = arguments.state or (
        Path(".yimby") / f"smoke-{authority}-{start}-{end}.json"
    )
    try:
        result = {
            "ok": True,
            **asyncio.run(_smoke(authority, package, start, end, state_path)),
        }
    except (ValueError, RuntimeError) as error:
        result = {
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error),
            "state": str(state_path),
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1
