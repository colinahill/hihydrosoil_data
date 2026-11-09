"""CF, spatial and multiscale metadata; physical units are stored directly."""

from pyproj import CRS

from . import config


def property_attrs(spec, factor=1):
    attrs = {
        "long_name": spec.long_name,
        "units": spec.units,
        "source_version": config.SOURCE_VERSION,
        "source_token": spec.source_token,
        "source_nodata": spec.source_nodata,
        "grid_mapping": "spatial_ref",
        "coordinates": "spatial_ref",
    }
    if spec.categorical:
        attrs.update(
            flag_values=list(spec.flags),
            flag_meanings=" ".join(spec.meanings),
            # JSON attribute keys are strings, even though raster codes are integers.
            class_codes={str(code): label for code, label in spec.class_codes.items()},
        )
    else:
        attrs.update(source_conversion_factor=10000, comment="Decoded float32 in physical units; NaN is nodata.")
    if factor > 1:
        attrs.update(
            overview_factor=factor,
            resampling_method="mode" if spec.categorical else "mean",
            derived_from="." if factor == 2 else f"{factor // 2}x",
            overview_note="Chained fill-aware reduction for display; use native pixels for analysis.",
        )
    return attrs


def spatial_attrs(grid, factor=1):
    h, w = grid.shape_at(factor)
    a = grid.transform(factor)
    return {
        "zarr_conventions": [
            {
                "schema_url": "https://raw.githubusercontent.com/zarr-conventions/geo-proj/refs/tags/v1/schema.json",
                "spec_url": "https://github.com/zarr-conventions/geo-proj/blob/v1/README.md",
                "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
                "name": "proj",
                "description": "Coordinate reference system information for geospatial data",
            },
            {
                "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v1/schema.json",
                "spec_url": "https://github.com/zarr-conventions/spatial/blob/v1/README.md",
                "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
                "name": "spatial",
                "description": "Spatial coordinate information",
            },
        ],
        "proj:epsg": 4326,
        "proj:wkt2": CRS(grid.crs).to_wkt(),
        "spatial:dimensions": ["y", "x"],
        "spatial:registration": "pixel",
        "spatial:transform": list(a)[:6],
        "spatial:shape": [h, w],
        "spatial:bbox": [a.c, a.f + a.e * h, a.c + a.a * w, a.f],
        "native_bbox": [
            grid.x_min,
            grid.y_max - grid.height * grid.pixel_size,
            grid.x_min + grid.width * grid.pixel_size,
            grid.y_max,
        ],
    }


def root_attrs():
    return {
        "title": "HiHydroSoil v2.0 (nominal 250 m) as Icechunk Zarr",
        "source": config.SOURCE_URL,
        "source_version": config.SOURCE_VERSION,
        "dataset_version": config.DATASET_VERSION,
        "institution": "FutureWater",
        "references": config.CITATION,
        "Conventions": "CF-1.10",
        "license": "CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
        "processing_code": "https://github.com/colinahill/hihydrosoil_data",
        "comment": "Native geographic grids preserved without resampling. Six standard depths; aggregates deferred. "
        "Continuous values decoded by division by 10000; categorical codes preserved. "
        "Topsoil is 0-30 cm and subsoil 30-200 cm; the source README swaps those labels.",
    }
