# Copyright (c) 2026 Kostas Stathoulopoulos

"""Content-addressed compressed source evidence."""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from yimby.domain import EvidenceCapture


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
