# Copyright (c) 2026 Kostas Stathoulopoulos

"""Devon County Council planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.devon.adapter import (
    DevonAdapter,
    DevonApplicationV1,
    DevonCheckpointV1,
    DevonDiscoveryScope,
    DevonQualificationAuditV1,
    DevonQuerySummaryV1,
)

DEVON_PACKAGE: AuthorityPackage[DevonApplicationV1, DevonCheckpointV1] = (
    AuthorityPackage(DevonAdapter(), DevonApplicationV1, DevonCheckpointV1)
)

__all__ = [
    "DEVON_PACKAGE",
    "DevonDiscoveryScope",
    "DevonQualificationAuditV1",
    "DevonQuerySummaryV1",
]
