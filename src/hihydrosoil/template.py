"""Additive store initialization; incompatible encodings or grids are refused."""

import numpy as np
import xarray as xr
import zarr
from icechunk.xarray import to_icechunk

from . import config, metadata


class EncodingMismatch(ValueError):
    """A structural change requires a new dataset version/store."""


def coords_dataset(grid, with_depth, factor=1):
    x, y = grid.coords(factor)
    from pyproj import CRS

    coords = {
        "x": ("x", x, {"standard_name": "longitude", "units": "degrees_east", "axis": "X"}),
        "y": ("y", y, {"standard_name": "latitude", "units": "degrees_north", "axis": "Y"}),
        "spatial_ref": (
            (),
            np.int64(0),
            CRS(grid.crs).to_cf()
            | {
                "crs_wkt": CRS(grid.crs).to_wkt(),
                "GeoTransform": " ".join(map(str, grid.transform(factor).to_gdal())),
            },
        ),
    }
    if with_depth:
        coords.update(
            {
                config.DEPTH_DIM: (config.DEPTH_DIM, np.array(config.DEPTH_LABELS)),
                "depth_top_cm": (
                    config.DEPTH_DIM,
                    np.array([d[0] for d in config.DEPTH_BOUNDS], dtype="int16"),
                    {"units": "cm"},
                ),
                "depth_bottom_cm": (
                    config.DEPTH_DIM,
                    np.array([d[1] for d in config.DEPTH_BOUNDS], dtype="int16"),
                    {"units": "cm"},
                ),
            }
        )
    return xr.Dataset(coords=coords)


def verify_array(arr, spec, factor=1):
    shape = config.GRIDS[spec.group].shape_at(factor)
    want = (spec.depths, *shape) if spec.ndim == 3 else shape
    fill_ok = arr.fill_value == spec.fill if spec.categorical else np.isnan(arr.fill_value)
    if (
        arr.shape != want
        or arr.dtype != np.dtype(spec.dtype)
        or not fill_ok
        or tuple(arr.metadata.dimension_names) != spec.dims
        or arr.chunks != config.ENCODING.chunks(spec.ndim)
        or arr.shards != config.ENCODING.shards(spec.ndim, spec.depths if factor == 1 else 1)
        or arr.metadata.codecs[0].codecs[-1].to_dict()
        != zarr.codecs.ZstdCodec(level=config.ENCODING.zstd_level).to_dict()
    ):
        raise EncodingMismatch(f"{arr.path}: incompatible grid/dtype/chunk/shard/codec; use a fresh versioned store")


def init_store(session, properties=None):
    properties = list(config.PROPERTIES.values()) if properties is None else properties
    root = zarr.open_group(session.store, mode="a")
    existing_version = root.attrs.get("dataset_version")
    if existing_version and existing_version != config.DATASET_VERSION:
        raise EncodingMismatch("store dataset version differs from config")
    created = []
    for group_name, grid in config.GRIDS.items():
        specs = [p for p in properties if p.group == group_name]
        if not specs:
            continue
        for factor in (1, *config.OVERVIEW_FACTORS):
            path = group_name if factor == 1 else f"{group_name}/{factor}x"
            if path not in root:
                to_icechunk(
                    coords_dataset(grid, group_name == "soil_properties", factor), session, group=path, mode="w"
                )
            gp = zarr.open_group(session.store, path=path, mode="r+")
            expected = metadata.spatial_attrs(grid, factor)
            for key in ("spatial:transform", "spatial:shape"):
                if key in gp.attrs and gp.attrs[key] != expected[key]:
                    raise EncodingMismatch(f"{path}: existing coordinate grid differs")
            # Check the actual coordinates as well, not only their metadata.
            x, y = grid.coords(factor)
            if not np.array_equal(gp["x"][:], x) or not np.array_equal(gp["y"][:], y):
                raise EncodingMismatch(f"{path}: existing coordinate values differ")
            gp.attrs.update(expected)
            if factor == 1:
                gp.attrs.update(
                    multiscales={
                        "layout": [
                            {"asset": ".", "resolution": [grid.pixel_size, grid.pixel_size]},
                            *[
                                {
                                    "asset": f"{f}x",
                                    "derived_from": "." if f == 2 else f"{f // 2}x",
                                    "resolution": [grid.pixel_size * f] * 2,
                                    "shape": list(grid.shape_at(f)),
                                    "resampling_method": "variable-specific: mean for continuous, mode for categorical",
                                }
                                for f in config.OVERVIEW_FACTORS
                            ],
                        ]
                    }
                )
            else:
                gp.attrs.update(overview_factor=factor, edge_policy="pad partial edge blocks with fill")
            for spec in specs:
                if spec.name in gp:
                    verify_array(gp[spec.name], spec, factor)
                    # Correct metadata on arrays initialized before the published
                    # filename spelling was verified. Data and progress are preserved.
                    if spec.name == "alpha" and gp[spec.name].attrs.get("source_token") == "ALPHA":
                        gp[spec.name].attrs["source_token"] = spec.source_token
                    if spec.categorical:
                        codes = metadata.property_attrs(spec, factor)["class_codes"]
                        if gp[spec.name].attrs.get("class_codes") != codes:
                            gp[spec.name].attrs["class_codes"] = codes
                    continue
                shape = grid.shape_at(factor)
                gp.create_array(
                    spec.name,
                    shape=(spec.depths, *shape) if spec.ndim == 3 else shape,
                    chunks=config.ENCODING.chunks(spec.ndim),
                    shards=config.ENCODING.shards(spec.ndim, spec.depths if factor == 1 else 1),
                    dtype=spec.dtype,
                    fill_value=spec.fill,
                    dimension_names=spec.dims,
                    compressors=[zarr.codecs.ZstdCodec(level=config.ENCODING.zstd_level)],
                    attributes=metadata.property_attrs(spec, factor),
                )
                created.append(f"{path}/{spec.name}")
    root.attrs.update(metadata.root_attrs())
    return created
