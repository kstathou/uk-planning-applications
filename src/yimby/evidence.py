# Copyright (c) 2026 Kostas Stathoulopoulos

"""Content-addressed compressed source evidence."""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from yimby.domain import EvidenceCapture, EvidenceDigest

if TYPE_CHECKING:
    from pathlib import Path


class EvidenceStore:
    """Write each response body once under its content digest."""

    def __init__(self, root: Path) -> None:
        """Set the evidence root without creating it eagerly."""
        self.root = root

    def put(self, capture: EvidenceCapture) -> Path:
        """Persist a deterministic gzip member and return its path."""
        directory = self.root / str(capture.digest)[:2]
        path = directory / f"{capture.digest}.gz"
        if path.exists():
            return path
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(capture.body, mtime=0))
        temporary.replace(path)
        return path

    def relative_path(self, path: Path) -> str:
        """Return a backup-portable path below the evidence root."""
        return str(path.relative_to(self.root))

    def read_capture(
        self,
        digest: EvidenceDigest,
        stored_path: str,
        source_url: str,
        media_type: str,
    ) -> EvidenceCapture:
        """Rehydrate retained evidence for offline normalisation."""
        candidate = self.root / stored_path
        return EvidenceCapture(
            url=HttpUrl(source_url),
            media_type=media_type,
            body=gzip.decompress(candidate.read_bytes()),
            digest=digest,
        )
