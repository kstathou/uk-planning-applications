# Copyright (c) 2026 Kostas Stathoulopoulos

"""Old Oak and Park Royal Development Corporation adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.opdc.adapter import (
    OpdcAdapter,
    OpdcApplicationV1,
    OpdcCheckpointV1,
)

OPDC_PACKAGE: AuthorityPackage[OpdcApplicationV1, OpdcCheckpointV1] = AuthorityPackage(
    OpdcAdapter(), OpdcCheckpointV1
)

__all__ = ["OPDC_PACKAGE"]
