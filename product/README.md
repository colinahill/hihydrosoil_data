# HiHydroSoil v2.0 Soil Hydraulic Properties

Global maps of how soil holds and transmits water, from
[FutureWater HiHydroSoil v2.0](https://www.futurewater.nl/solutions/hihydrosoil/).
The data include water content, hydraulic conductivity, organic matter, and soil
texture at six depths from the surface to 2 m, plus hydrologic soil groups.

This product stores the maps in [Icechunk](https://icechunk.io) Zarr v3, so you can
read a location or small area without downloading the global GeoTIFFs. Values
are in physical units, ready to use. The original grids are preserved, at a
nominal resolution of **250 m**.

## Contents

The store is at `chill/hihydrosoil/v0.1.0.icechunk/`:

| Group | Dimensions | Contents |
|---|---|---|
| `soil_properties` | `(depth_interval, y, x)` | 13 soil properties and a texture class, at six depths |
| `profile_properties` | `(y, x)` | Hydrologic soil group, with one value per location |
| `{group}/2x` through `{group}/256x` | Same dimensions, fewer pixels | Coarser maps for display |

The two main groups use separate grids. Both use WGS84 longitude and latitude
(EPSG:4326): `x` is longitude and `y` is latitude. Coordinates mark pixel centers,
with latitude running from north to south.

| Grid | `soil_properties` | `profile_properties` |
|---|---|---|
| Shape `(y, x)` | 60,934 × 158,159 | 79,080 × 158,159 |
| Pixel spacing | 0.002276188° | 0.002276188° |
| Latitude extent | About 55.98°S to 82.72°N | About 90°S to 90°N |

Pixel sizes on the ground vary with latitude. The grids also have different
origins, so select locations separately in each group.

Continuous properties are **float32 in physical units**, with **NaN for missing data**.
The source scaling has already been applied; no conversion is needed. Soil texture
is int16 with `-9999` for missing data. Hydrologic soil group is uint8 with `255`
for missing data. Units, class labels, and source information are recorded on each
array; `spatial_ref` records the coordinate system.

## Included properties

All properties below have six depth layers except `hydrologic_soil_group`, which
describes the soil profile as a whole.

| Array | Units | Meaning |
|---|---|---|
| `wcsat` | m³/m³ | Water content at saturation |
| `wcres` | m³/m³ | Residual water content |
| `wcpf2` | m³/m³ | Field capacity: water content at pF2 |
| `wcpf3` | m³/m³ | Critical point: water content at pF3 |
| `wcpf42` | m³/m³ | Permanent wilting point: water content at pF4.2 |
| `wcavail` | m³/m³ | Available water content: field capacity minus wilting point |
| `sat_field` | m³/m³ | Water content between saturation and field capacity |
| `field_crit` | m³/m³ | Water content between field capacity and the critical point |
| `crit_wilt` | m³/m³ | Water content between the critical and wilting points |
| `ksat` | cm/day | Saturated hydraulic conductivity: how readily water moves through saturated soil |
| `alpha` | 1/cm | Parameter α in the Mualem–van Genuchten water-retention model |
| `n` | Dimensionless | Parameter n in the Mualem–van Genuchten water-retention model |
| `ormc` | % | Organic matter content |
| `stc` | Integer code | Simplified FAO soil texture class |
| `hydrologic_soil_group` | Integer code | Runoff classification for the soil profile |

Water content is a volume fraction: **0.30 m³/m³ means water occupies 30% of the
soil volume**. The pF value describes how tightly water is held in the soil;
higher values represent drier conditions.

### Depth intervals

| `depth_interval` | Top, cm | Bottom, cm |
|---|---:|---:|
| `0_5` | 0 | 5 |
| `5_15` | 5 | 15 |
| `15_30` | 15 | 30 |
| `30_60` | 30 | 60 |
| `60_100` | 60 | 100 |
| `100_200` | 100 | 200 |

The `depth_top_cm` and `depth_bottom_cm` coordinates give these boundaries as
numbers. The source's separate topsoil and subsoil summary layers are not included.

### Class codes

| `stc` code | Texture |
|---|---|
| 1 | Organic |
| 2 | Very fine |
| 3 | Fine |
| 4 | Medium fine |
| 5 | Coarse |
| 6 | Medium |
| -9999 | No data |

| Hydrologic soil group code | Group | Meaning |
|---|---|---|
| 1 | A | Low runoff potential |
| 2 | B | Moderately low runoff potential |
| 3 | C | Moderately high runoff potential |
| 4 | D | High runoff potential |
| 14 | A/D | Dual group |
| 24 | B/D | Dual group |
| 34 | C/D | Dual group |
| 255 | — | No data |

For A/D, B/D, and C/D, the first letter applies when the soil is drained; D applies
when it is undrained. Each array has a `class_codes` dictionary: STC maps codes to
texture names, while hydrologic soil group maps codes to a `group` and `meaning`.
The dictionaries cover valid classes; missing-data values are listed above.
Dictionary keys are strings because attributes use JSON. The `flag_values` and
`flag_meanings` attributes provide the same class codes in CF format. Xarray
preserves integer codes, including the missing-data values.

## Reading the data

Read access is public and requires no credentials. With `icechunk`, `xarray`, and
`zarr` installed, open the store and select a location:

```python
import icechunk
import xarray as xr

storage = icechunk.s3_storage(
    bucket="chill", prefix="hihydrosoil/v0.1.0.icechunk",
    region="us-east-1", endpoint_url="https://data.source.coop",
    anonymous=True, force_path_style=True,
)
repo = icechunk.Repository.open(storage)
session = repo.readonly_session("main")
soil = xr.open_zarr(
    session.store, group="soil_properties", consolidated=False, chunks=None,
)

# All six depths at a WGS84 location; x is longitude, y is latitude.
profile = soil[["wcpf2", "wcavail", "ksat"]].sel(
    x=-102.45, y=41.55, method="nearest",
)
profile.to_array().plot(col="variable", sharey=False)

# A small area, about 4 × 4 km at this latitude, in the surface layer.
# Latitude slices run from north to south.
field = soil["wcpf2"].sel(
    depth_interval="0_5",
    x=slice(-102.475, -102.425), y=slice(41.568, 41.532),
)
field
```

Open hydrologic soil group separately:

```python
groups = xr.open_zarr(
    session.store, group="profile_properties", consolidated=False, chunks=None,
)
code = int(groups["hydrologic_soil_group"].sel(
    x=-102.45, y=41.55, method="nearest",
).item())
label = groups["hydrologic_soil_group"].attrs["class_codes"].get(str(code), "No data")
label
```

Reuse the repository and session for repeated reads.

## Storage layout

Arrays are split into small chunks, grouped into larger files called shards.
A query downloads only the chunks it needs within each shard.

| Arrays | Inner chunks | Shards | Uncompressed shard size |
|---|---|---|---:|
| Native continuous arrays | `(1, 64, 64)` | `(6, 1024, 1024)` | 24 MiB |
| Continuous overviews | `(1, 64, 64)` | `(1, 1024, 1024)` | 4 MiB |
| Native STC (int16) | `(1, 64, 64)` | `(6, 1024, 1024)` | 12 MiB |
| STC overviews | `(1, 64, 64)` | `(1, 1024, 1024)` | 2 MiB |
| Hydrologic soil group, all levels | `(64, 64)` | `(1024, 1024)` | 1 MiB |

Chunks are compressed with Zstd. One float32 chunk is 16 KiB before compression.
A native shard in `soil_properties` holds all six depths, so they can be read together.

## Overviews

Levels `2x` through `256x` are child groups of both `soil_properties` and
`profile_properties`. Each has the same variables and depth labels as its parent,
with coarser coordinates. For example, a `64x` pixel spans 64 native pixels in
each direction. Open any level in the same way as the native group:

```python
coarse = xr.open_zarr(
    session.store, group="soil_properties/64x", consolidated=False, chunks=None,
)
map_layer = coarse["wcpf2"].sel(depth_interval="0_5")
```

| Level | Soil properties grid `(y × x)` | Hydrologic group grid `(y × x)` | Nominal pixel size |
|---|---:|---:|---:|
| Native | 60,934 × 158,159 | 79,080 × 158,159 | 250 m |
| `2x` | 30,467 × 79,080 | 39,540 × 79,080 | 500 m |
| `4x` | 15,234 × 39,540 | 19,770 × 39,540 | 1 km |
| `8x` | 7,617 × 19,770 | 9,885 × 19,770 | 2 km |
| `16x` | 3,809 × 9,885 | 4,943 × 9,885 | 4 km |
| `32x` | 1,905 × 4,943 | 2,472 × 4,943 | 8 km |
| `64x` | 953 × 2,472 | 1,236 × 2,472 | 16 km |
| `128x` | 477 × 1,236 | 618 × 1,236 | 32 km |
| `256x` | 239 × 618 | 309 × 618 | 64 km |

Pixel sizes are nominal; ground distances vary with latitude. Partial edge blocks
are retained, so grid dimensions are rounded up at each level.

Continuous overviews average the available values; class maps use the most common
available code. Missing values are excluded. Each level is built from the
previous one, so an overview can differ from an average or most-common class
calculated directly from the original pixels. **Use native pixels for analysis.**

## Versioning

The store path identifies product version **0.1.0**; the source version is **2.0**.
New source releases or incompatible layouts use a new store path, leaving earlier
versions readable. There is no time dimension; each source release is a separate
version of the dataset.

The `main` branch can change as data are added or corrected. For repeatable
results, save the snapshot ID used by your analysis and reopen that snapshot:

```python
snapshot_id = session.snapshot_id
session = repo.readonly_session(snapshot_id=snapshot_id)
```

Release tags, such as `hihydrosoil-2.0.0`, also identify fixed snapshots. Tags are
never deleted or reused; a corrected release gets a new tag with a suffix such
as `-r2`.

## Provenance and license

Built from FutureWater's HiHydroSoil v2.0 GeoTIFFs, preserving their original
grids. FutureWater estimated the hydraulic properties from soil characteristics
using relationships called pedotransfer functions. Some gaps were filled from
HiHydroSoil v1.2.

This product restores physical units by dividing continuous source values by
10,000 and preserves integer class codes. Each array records its units, source
version, and source layer name. Validation compares sampled pixels with the
original TIFFs and checks overview calculations independently. Float32 rounding
errors are recorded in the validation reports. The audit location for source
inventories and reports is `audit/2.0.0/`.

Processing code (MIT licensed):
https://github.com/colinahill/hihydrosoil_data

Source data: [FutureWater HiHydroSoil v2.0](https://www.futurewater.nl/solutions/hihydrosoil/),
licensed [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/). Please cite:

> Simons, G.W.H., R. Koster, P. Droogers. 2020. HiHydroSoil v2.0 — A high
> resolution soil map of global hydraulic properties. FutureWater Report 213.

See the [data reference](https://github.com/colinahill/hihydrosoil_data/blob/main/docs/data-reference.md)
for technical details and [planned additions](https://github.com/colinahill/hihydrosoil_data/blob/main/docs/future-variables.md)
for layers outside this product.
