# HiHydroSoil v2.0 data reference

This specification distinguishes measurements from the locally available TIFFs
from expected properties of files still to be downloaded. Source authority:
[FutureWater's dataset page](https://www.futurewater.nl/solutions/hihydrosoil/),
the supplied October 2020 report, and `Readme_HHSv2.txt`.

## Product and measured grids

```text
soil_properties/                    (depth_interval[6], y, x)
  alpha n ksat ormc stc wcsat wcres wcpf2 wcpf3 wcpf42 wcavail
  sat_field field_crit crit_wilt
  2x/ 4x/ 8x/ 16x/ 32x/ 64x/ 128x/ 256x/
profile_properties/                 (y, x), separate grid
  hydrologic_soil_group
  2x/ ... 256x/
```

All twelve local CRIT-WILT/WCpF2 TIFFs have exactly matching transforms and shape.
Their canonical `soil_properties` grid is measured, not inferred from SoilGrids.
HSG uses its own measured grid; it is offset by 3198.644 rows and cannot be placed
on the six-depth grid by an integer translation.

| Measurement | Six-depth grid | HSG grid |
|---|---|---|
| CRS | EPSG:4326 | EPSG:4326 |
| Shape `(y, x)` | `(60934, 158159)` | `(79080, 158159)` |
| Upper-left x edge | `-179.99999985285478` | `-180.0` |
| Upper-left y edge | `82.71928405344526` | `90.0` |
| Pixel step x / y | `+0.00227618823616542 / -0.00227618823616542` degrees | Same |
| Source type / nodata | Continuous: int32 / -9999; STC: int16 / -9999 | uint8 / 255 |
| GeoTIFF blocks | 256 x 256 | 256 x 256 |

These are geographic rasters, so "250 m" is nominal: physical east-west lengths
vary with latitude. Source bounds, including HSG's slightly south-of-90-degree
bottom edge, are preserved. Coordinate arrays are float64 pixel centers, x
ascending and y descending. CF `spatial_ref` carries the WKT and GeoTransform.
The published sibling SoilGrids store uses a different Interrupted Goode
Homolosine grid, measured at `(59400, 160200)` with 250 m spacing. Matching its
chunk dimensions does not align spatial pixels.

Source dtypes, nodata, and canonical-grid assumptions for the other twelve
six-depth properties must pass `inspect-source` when downloaded. The pipeline
refuses unexpected headers; it does not silently cast or reproject them.

## Depths and variables

`depth_interval` labels are `0_5`, `5_15`, `15_30`, `30_60`, `60_100`, and
`100_200`; numeric `depth_top_cm` / `depth_bottom_cm` give boundaries in cm.
Categorical STC has the same depth coordinate. HSG is a profile classification,
not six replicated depth slices.

| Variable | Source token | Stored units / meaning |
|---|---|---|
| `ormc` | ORMC | Organic matter, % |
| `stc` | STC | Simplified FAO texture codes: 1 organic, 2 very fine, 3 fine, 4 medium fine, 5 coarse, 6 medium |
| `alpha` | ALFA | MVG alpha, cm-1 |
| `n` | N | MVG n, dimensionless |
| `wcsat` | WCsat | Saturated water content, m3 m-3 |
| `wcres` | WCres | Residual water content, m3 m-3 |
| `ksat` | Ksat | Saturated hydraulic conductivity, cm d-1 |
| `wcpf2` | WCpF2 | Field-capacity water content (pF2), m3 m-3 |
| `wcpf3` | WCpF3 | Critical water content (pF3), m3 m-3 |
| `wcpf42` | WCpF4.2 | Wilting-point water content (pF4.2), m3 m-3 |
| `wcavail` | WCavail | Available water content, m3 m-3 |
| `sat_field` | SAT-FIELD | Saturation minus field-capacity water content, m3 m-3 |
| `field_crit` | FIELD-CRIT | Field-capacity minus critical water content, m3 m-3 |
| `crit_wilt` | CRIT-WILT | Critical minus wilting-point water content, m3 m-3 |
| `hydrologic_soil_group` | Hydrologic_Soil_Group | uint8 codes 1 A, 2 B, 3 C, 4 D, 14 A/D, 24 B/D, 34 C/D; 255 nodata |

The report describes hydraulic variables derived with pedotransfer functions
from SoilGrids 2.0 inputs. Missing input regions were supplemented from
HiHydroSoil 1.2. This repository republishes the provided rasters; it does not
recompute the scientific models, gap filling, texture classes or HSG.

Continuous source int32 values are divided by float32 10000. Nodata becomes NaN.
No CF `scale_factor` or `_FillValue` is attached to those arrays, so browser
clients and xarray both see physical units. STC retains int16 category codes
and -9999 nodata;
HSG retains uint8 codes/nodata. Category attributes include `flag_values`,
`flag_meanings`, and `class_codes`, without CF masking that would upcast categories
to float. STC maps codes to texture names; HSG maps codes to dictionaries with
`group` and `meaning`. JSON keys are strings; missing-data codes are excluded.
Re-running `init-store` adds or corrects these mappings on existing arrays at all
levels without changing data or progress.

Float32 is not a lossless encoding of every scaled int32. Validation compares
exactly against the specified conversion and separately reports absolute
conversion errors and failed integer round trips. CRIT-WILT sample values
previously inspected at all six depths round-tripped, but that result does not
establish exact invertibility for ALFA/N/Ksat or all geographic locations.

## Encoding and overview computation

| Array | Inner chunks | Shards | Raw shard size |
|---|---|---|---|
| Native continuous arrays | `(1, 64, 64)` | `(6, 1024, 1024)` | 24 MiB |
| Continuous overview arrays | `(1, 64, 64)` | `(1, 1024, 1024)` | 4 MiB |
| Native STC | `(1, 64, 64)` | `(6, 1024, 1024)` | 12 MiB |
| STC overviews | `(1, 64, 64)` | `(1, 1024, 1024)` | 2 MiB |
| Native / overview HSG | `(64, 64)` | `(1024, 1024)` | 1 MiB |

Zstd level 3 compresses each inner chunk independently. One float32 chunk is
16 KiB raw. Native six-depth shard indexes are approximately 24 KiB. Sharding
permits range reads of selected inner chunks. A nominal chunk footprint is
16 x 16 km and a shard footprint 256 x 256 km; native angular lengths are
0.145676047 degrees and 2.330816754 degrees respectively.

Levels use chained stride-two mean for continuous arrays and mode for categories.
Topozarr's Rust `block_reduce` is called directly for mean, with fill/skipna
explicit. Its high-level pyramid writer is not used. A custom NumPy category
reducer excludes nodata and resolves ties to the lowest numeric code.

Work is one destination shard at one depth. Parent windows are at most
2048 x 2048, not an entire global array. Materialization records data-bearing
coverage separately from processed all-fill shards; pyramids skip irrelevant
ocean regions. Historical coverage ensures rebuilds visit old regions too.
Checkpoint tokens identify the native revision, source fingerprint, factor
ladder, and count of finished regions at each level.

Odd parent edges are padded with fill. Level shape is `ceil(native / factor)`;
all source pixels are retained. Full coarse edge cells can extend past the
original extent; `native_bbox` records the original extent. Arithmetic means
weight valid parent pixels equally, not by geographic area. Chained means
with unequal valid counts differ from a direct native-pixel mean; chained
category modes also differ from direct full-resolution modes.

## Identity, validation and release

Manifest rows record source path, property/depth, header, size, nanosecond mtime,
and optional SHA-256. Files are checked again before and after write batches.
Manifest identity is attached to native progress; changed source identity is
refused on an existing store. Keep downloaded files stable throughout processing.
Resume skips only data committed with its completion ledger. Failed batches
are rerun; uncommitted objects can later be reclaimed by garbage collection.

Validation checks actual coordinates/CRS, shapes and encodings, source pixels
from meaningful completed windows, and independent overview reductions. It
records the checked snapshot. Release requires the whole configured inventory,
complete native coverage, complete overview scopes, and a full passing report
for that exact snapshot. Tags are immutable. No remote publication is automatic.

Source use requires attribution. The current FutureWater page identifies
CC-BY 4.0; the bundled README requires citation of Report 213. The PDF's last
citation footnote says Report 134, which refers to v1.2; use **213** for v2.0.
