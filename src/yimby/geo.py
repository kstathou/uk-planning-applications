# Copyright (c) 2026 Kostas Stathoulopoulos

"""Planning-coordinate conversion at the normalisation boundary."""

from pyproj import Transformer

from yimby.domain import ApplicationLocation, Wgs84Coordinate

_BNG_TO_WGS84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def bng_to_wgs84(
    easting: float | None,
    northing: float | None,
) -> ApplicationLocation | None:
    """Project a complete BNG pair, leaving missing locations unknown."""
    if easting is None or northing is None:
        return None
    longitude, latitude = _BNG_TO_WGS84.transform(
        easting,
        northing,
        direction="FORWARD",
    )
    return ApplicationLocation(
        bng_easting=easting,
        bng_northing=northing,
        wgs84=Wgs84Coordinate(longitude=longitude, latitude=latitude),
    )
