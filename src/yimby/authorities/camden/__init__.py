# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.camden.adapter import (
    CamdenAdapter,
    CamdenApplicationV1,
    CamdenCheckpointV1,
)

CAMDEN_PACKAGE: AuthorityPackage[CamdenApplicationV1, CamdenCheckpointV1] = (
    AuthorityPackage(CamdenAdapter(), CamdenApplicationV1, CamdenCheckpointV1)
)

__all__ = ["CAMDEN_PACKAGE"]
