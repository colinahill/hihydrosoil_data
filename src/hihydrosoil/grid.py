"""Native-grid checks and deterministic shard windows, including partial edges."""

from __future__ import annotations

import math

from pyproj import CRS

from . import config


def verify_source(src, spec: config.PropertySpec):
    grid = config.GRIDS[spec.group]
    if CRS(src.crs) != CRS(grid.crs) or src.shape != grid.shape:
        raise ValueError(f"{src.name}: CRS/shape differs from configured {spec.group} grid")
    if any(abs(a - b) > 1e-10 for a, b in zip(src.transform, grid.transform(), strict=True)):
        raise ValueError(f"{src.name}: affine transform differs from configured {spec.group} grid")
    if src.count != 1 or src.dtypes[0] != spec.source_dtype or src.nodata != spec.source_nodata:
        raise ValueError(
            f"{src.name}: expected one {spec.source_dtype} band with nodata {spec.source_nodata}; "
            f"got {src.count} band(s), dtype {src.dtypes[0]}, nodata {src.nodata}"
        )


def windows(shape, *, bbox=None, grid=None):
    """Return disjoint, whole-shard windows intersecting a WGS84 bbox."""
    h, w = shape
    sy, sx = config.ENCODING.shard_y, config.ENCODING.shard_x
    y0, y1, x0, x1 = 0, h, 0, w
    if bbox is not None:
        west, south, east, north = bbox
        if grid is None or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be west,south,east,north in WGS84, without crossing the antimeridian")
        x0 = max(0, math.floor((west - grid.x_min) / grid.pixel_size))
        x1 = min(w, math.ceil((east - grid.x_min) / grid.pixel_size))
        y0 = max(0, math.floor((grid.y_max - north) / grid.pixel_size))
        y1 = min(h, math.ceil((grid.y_max - south) / grid.pixel_size))
    return (
        [
            (y, min(y + sy, h), x, min(x + sx, w))
            for y in range(y0 // sy * sy, y1, sy)
            for x in range(x0 // sx * sx, x1, sx)
        ]
        if x0 < x1 and y0 < y1
        else []
    )


def window_key(window):
    return f"{window[0] // config.ENCODING.shard_y}-{window[2] // config.ENCODING.shard_x}"


def parse_bbox(value):
    if not value:
        return None
    parts = tuple(float(v) for v in value.split(","))
    if len(parts) != 4 or not all(math.isfinite(v) for v in parts):
        raise ValueError("bbox must contain four finite coordinates")
    return parts
