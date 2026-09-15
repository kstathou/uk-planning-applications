# Copyright (c) 2026 Kostas Stathoulopoulos

"""Leeds planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.leeds.adapter import (
    LeedsAdapter,
    LeedsApplicationV1,
    LeedsCheckpointV1,
)

LEEDS_PACKAGE: AuthorityPackage[LeedsApplicationV1, LeedsCheckpointV1] = (
    AuthorityPackage(LeedsAdapter(), LeedsCheckpointV1)
)

__all__ = ["LEEDS_PACKAGE"]
