# HiHydroSoil v2.0 to Icechunk

Turn manually downloaded, extracted [FutureWater HiHydroSoil v2.0](https://www.futurewater.nl/solutions/hihydrosoil/)
GeoTIFFs into an analysis-ready Icechunk Zarr v3 store. Preserve the source
geographic grids without resampling or reprojection. Continuous values are decoded
float32 in physical units, with NaN for nodata. Categories remain integers.

- [Data reference](docs/data-reference.md): grids, variables, units, encodings,
  overview behavior, validation and provenance.
- [Deferred variables](docs/future-variables.md): topsoil/subsoil aggregates.
- [Product README](product/README.md): Source Cooperative landing page and consumer examples.

## Setup and source data

Install Python 3.13 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From the repository root, run:

```bash
uv sync --locked
make test
make lint
```

`make test` uses pytest-xdist with one worker per logical CPU core.

Obtain the download links from [FutureWater](https://www.futurewater.nl/solutions/hihydrosoil/).
Download the data separately, using curl or a browser, and place the extracted
GeoTIFFs under `data/` (gitignored). The pipeline reads local TIFFs directly.

For example:

```text
data/
  crit-wilt/
    CRIT-WILT_0-5cm_M_250m.tif
    CRIT-WILT_5-15cm_M_250m.tif
    CRIT-WILT_15-30cm_M_250m.tif
    CRIT-WILT_30-60cm_M_250m.tif
    CRIT-WILT_60-100cm_M_250m.tif
    CRIT-WILT_100-200cm_M_250m.tif
  wcpf2/
    WCpF2_0-5cm_M_250m.tif
    ...
  Hydrologic_Soil_Group_250m.tif
```

Folder names can vary; files are discovered recursively by their source filenames.
Each selected six-depth property needs all six TIFFs. Use `SOURCE=/path/to/data`
to read files stored elsewhere. See the data reference for the full property list.

The published topsoil/subsoil aggregates are deferred. All 14 properties at the
six standard depths are supported, including soil texture class. Hydrologic soil
group has its own 2-D `profile_properties` group and native grid.

## Pipeline

Make targets wrap the `hihydrosoil` Typer CLI (`uv run hihydrosoil --help`).
All store targets default to `./hihydrosoil_store_local/v0.1.0.icechunk`.
`ACCOUNT=chill` explicitly switches to the Source Cooperative product
`chill/hihydrosoil/v0.1.0.icechunk`. Generic S3 destinations are not supported.

### 1. Inspect the local source (optional)

```bash
make inspect-source SOURCE=data
# Inspect only these properties, skipping checksums for a faster trial:
make inspect-source PROPERTIES=crit_wilt,wcpf2 NO_CHECKSUMS=1
```

Verify every recognized file's shape, CRS, affine transform, band count, dtype and
nodata. Write `work/2.0.0/source_manifest.parquet` and `source_report.json` atomically.
Default inspection inventories available downloads and reports missing layers;
explicit `PROPERTIES=` requires all expected depths for those properties.
Duplicate property/depth files are refused. Unknown/aggregate TIFFs are reported
as ignored. SHA-256 is enabled by default; `NO_CHECKSUMS=1` still fingerprints file
size and nanosecond mtime. Hashing large local rasters takes an extra sequential
read. Materialization refuses sources changed since inspection.

### 2. Initialize the store

```bash
make init-store
```

Create the complete empty native and overview structure: metadata, small 1-D
coordinate arrays, and fill-valued sharded arrays. Re-running adds only missing
arrays. Existing coordinates, shape, dtype, fill, chunks, shards and compressor
must match configuration; structural changes require a new versioned store.
Re-running also adds or corrects the `class_codes` attributes for soil texture
and hydrologic soil group at every level, preserving data and processing progress.

### 3. Materialize native arrays

```bash
make materialize PROPERTIES=crit_wilt,wcpf2 BBOX=-102.46,41.54,-102.44,41.56
make materialize PROPERTIES=crit_wilt,wcpf2 WORKERS=4 COMMIT_EVERY=8
```

Materialization creates the manifest if needed and inspects selected properties
missing from it. Existing entries and their checksum policy are preserved; reruns
check file size and mtime without hashing again. Use `SOURCE=/path/to/data` for
downloads outside `data/`, and `NO_CHECKSUMS=1` to skip hashing a new manifest.
Changed sources still require explicit inspection and a fresh store version.

Every six-depth property requires all six source files before writing begins. The unit of
work is a spatial 1024 x 1024 shard across all six depths: 24 MiB uncompressed
for continuous variables or 12 MiB for STC.
Rasterio reads are bounded windows, with independent handles per task; no global
raster is loaded into memory. The final right/bottom shards are clipped to the
native array. BBOX selects whole intersecting shards, so the trial includes more
than the exact requested rectangle.

Each completed batch commits both data and its resume ledger. Re-runs skip already
completed shards. All-fill shards remain unwritten but count as processed. Source
identity changes require a fresh version; `OVERWRITE=1` reprocesses selected shards
of the same inspected source and invalidates their overview completion. It also
clears old data wherever the source is fill. Writers on disjoint properties can
rebase commits; use one writer per property and do not alter a property's native
data while building its overviews.

### 4. Generate overviews

```bash
make overviews PROPERTIES=crit_wilt,wcpf2 WORKERS=4 COMMIT_EVERY=8
```

Build `2x`, `4x`, `8x`, `16x`, `32x`, `64x`, `128x`, and `256x` as child groups.
Each is a chained stride-two reduction of the preceding level. Continuous values
use topozarr 0.1.6's Rust NaN-aware mean kernel directly; categories use a separate
fill-aware mode reducer, with ties resolved to the lowest code. Native stays at
the group root. Overview chunks are `(1, 64, 64)` and shards `(1, 1024, 1024)`.

Each task reads one depth's bounded parent window and writes a destination shard.
Only regions intersecting native data coverage are visited. Historical coverage
is retained so a rebuild can clear stale pixels. Progress is checkpointed per
level and keyed to the source/native revision and factor ladder. An interrupted
run resumes at its committed task; changing the native scope restarts reductions.
`REBUILD=1` explicitly reruns them. Partial-native pyramids never qualify for release.

Odd edge blocks are padded with nodata and retained.
Mean-of-means with unequal valid counts is not a direct native-pixel mean.
**Use native arrays for analysis; overviews are for display.**

### 5. Check status and validate

```bash
make status
make validate PROPERTIES=crit_wilt,wcpf2 SAMPLES=8 SEED=0
# One-shard trials have only one candidate source window:
make validate PROPERTIES=crit_wilt,wcpf2 SAMPLES=1
```

Structural checks cover all levels and coordinates. Source windows are sampled
from completed nonempty shards and compared exactly to the declared float32
conversion, or to raw categorical codes. Reports include maximum float conversion
error and integer round-trip differences. Some int32 source values cannot be
recovered exactly after float32 conversion.
Overview windows are independently recomputed, including ragged edges. Sampling
fails if it finds fewer meaningful windows than requested. `STRUCTURE_ONLY=1`
is available for metadata checks and cannot authorize release.

### 6. Release

```bash
make validate
make release
```

Release refuses missing native coverage, missing overviews, partial scopes,
structure-only validation, or a validation report for an older snapshot. A full
passing validation on the current snapshot creates immutable tag
`hihydrosoil-2.0.0`, then reopens it and checks structure. Existing tags are never
deleted or reused; a corrected release uses `SUFFIX=-r2`.

## Source Cooperative

Create the `hihydrosoil` product in account `chill` and authenticate with
`source-coop login`. The pipeline refreshes the CLI's cached credentials,
with JSON export support via
`CREDS_FILE=creds.json`. No credentials belong in Git.

```bash
make init-store ACCOUNT=chill
make materialize ACCOUNT=chill PROPERTIES=crit_wilt
make overviews ACCOUNT=chill PROPERTIES=crit_wilt
make validate ACCOUNT=chill PROPERTIES=crit_wilt
make publish-readme ACCOUNT=chill
make upload-audit ACCOUNT=chill
```

Writes go directly to the selected store; no automatic local-to-remote copy is
performed. Local is the default staging target. The CLI enables a 128 MiB
Icechunk byte cache; consumers can tune caching when opening the repository.

`make info`, `make show-config`, and `make help` expose the structure and settings.
`make garbage-collect` reclaims unreferenced objects; run it only with no active
writers. `clean-local-store` and `clean-work` remove generated pipeline outputs.
`clean-remote-store` removes the versioned remote prefix and requires an
interactive terminal plus two confirmations. Downloaded source TIFFs are never
automatically deleted.

## Updates

`SOURCE_VERSION=2.0.0` and `DATASET_VERSION=0.1.0` are independent. New source
identity, grids, chunking or semantics require a new dataset version/store path.
Adding a supported property to a partial build uses `init-store` followed by
materialization; completion and release still require the full configured inventory.
Code is MIT; source data is CC-BY 4.0. Cite FutureWater Report **213**, not 134.
