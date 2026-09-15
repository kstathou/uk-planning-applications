# Copyright (c) 2026 Kostas Stathoulopoulos

"""Peak District National Park planning adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.peak_district.adapter import (
    PeakDistrictAdapter,
    PeakDistrictApplicationV1,
    PeakDistrictCheckpointV1,
)

PEAK_DISTRICT_PACKAGE: AuthorityPackage[
    PeakDistrictApplicationV1, PeakDistrictCheckpointV1
] = AuthorityPackage(
    PeakDistrictAdapter(), PeakDistrictApplicationV1, PeakDistrictCheckpointV1
)

__all__ = ["PEAK_DISTRICT_PACKAGE"]
