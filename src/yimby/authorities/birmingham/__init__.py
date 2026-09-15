# Copyright (c) 2026 Kostas Stathoulopoulos

"""Birmingham planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.birmingham.adapter import (
    BirminghamAdapter,
    BirminghamApplicationV1,
    BirminghamCheckpointV1,
)

BIRMINGHAM_PACKAGE: AuthorityPackage[
    BirminghamApplicationV1, BirminghamCheckpointV1
] = AuthorityPackage(
    BirminghamAdapter(), BirminghamApplicationV1, BirminghamCheckpointV1
)

__all__ = ["BIRMINGHAM_PACKAGE"]
