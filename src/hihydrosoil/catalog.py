"""Inspect manually downloaded, extracted TIFFs. No network or archive handling."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import polars as pl
import rasterio

from . import config, grid

PATTERN = re.compile(r"^(.+)_(\d+-\d+cm)_M_250m\.tiff?$", re.IGNORECASE)


def normalise(token):
    return re.sub(r"[^a-z0-9]", "", token.lower())


def identify(path):
    token = path.stem
    if normalise(token) == "hydrologicsoilgroup250m":
        return config.PROPERTIES["hydrologic_soil_group"], "profile"
    match = PATTERN.match(path.name)
    if match is None:
        return None
    token, depth = match.groups()
    for spec in config.PROPERTIES.values():
        if normalise(token) == normalise(spec.source_token):
            if depth not in config.DEPTH_TOKENS:
                raise ValueError(f"unexpected depth: {path}")
            return spec, config.DEPTH_LABELS[config.DEPTH_TOKENS.index(depth)]
    return None


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_source(source, work_dir, properties=None, *, checksums=True):
    manifest, report = _scan_source(source, properties, checksums=checksums)
    _write_manifest(manifest, report, work_dir)
    return report


def _scan_source(source, properties=None, *, checksums=True):
    source = Path(source).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    selected = {p.name for p in properties} if properties is not None else set(config.PROPERTIES)
    rows, ignored, seen = [], [], set()
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".tif", ".tiff"}:
            continue
        found = identify(path)
        if found is None:
            ignored.append(str(path.relative_to(source)))
            continue
        spec, depth = found
        if spec.name not in selected:
            continue
        key = spec.name, depth
        if key in seen:
            raise ValueError(f"duplicate property/depth {key}: {path}")
        seen.add(key)
        with rasterio.open(path) as src:
            grid.verify_source(src, spec)
            stat = path.stat()
            rows.append(
                {
                    "property": spec.name,
                    "depth": depth,
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": checksum(path) if checksums else "",
                    "dtype": src.dtypes[0],
                    "nodata": int(src.nodata),
                    "width": src.width,
                    "height": src.height,
                    "crs": src.crs.to_string(),
                    "transform": list(src.transform)[:6],
                }
            )
    if not rows:
        raise ValueError(f"no recognised HiHydroSoil TIFFs in {source}")
    missing = [
        f"{spec.name}/{depth}"
        for spec in config.PROPERTIES.values()
        if spec.name in selected
        for depth in (config.DEPTH_LABELS if spec.ndim == 3 else ("profile",))
        if (spec.name, depth) not in seen
    ]
    if properties is not None and missing:
        raise ValueError(f"selected sources are incomplete: {', '.join(missing)}")
    manifest = pl.DataFrame(rows)
    report = {
        "source": str(source),
        "source_version": config.SOURCE_VERSION,
        "files": len(rows),
        "missing": missing,
        "ignored_tiffs": ignored,
        "checksums": checksums,
        "grids": {name: g.model_dump() for name, g in config.GRIDS.items()},
    }
    return manifest, report


def _write_manifest(manifest, report, work_dir):
    out = Path(work_dir) / config.SOURCE_VERSION
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "source_manifest.parquet.tmp"
    manifest.write_parquet(tmp)
    tmp.replace(out / "source_manifest.parquet")
    tmp = out / "source_report.json.tmp"
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(out / "source_report.json")


def ensure_manifest(work_dir, properties, *, source=None, checksums=True):
    """Inspect missing properties without refreshing existing source identities."""
    out = Path(work_dir) / config.SOURCE_VERSION
    manifest = read_manifest(work_dir) if (out / "source_manifest.parquet").exists() else None
    needed = []
    for spec in properties:
        if manifest is not None:
            rows = manifest.filter(pl.col("property") == spec.name).to_dicts()
            verify_fresh(rows)
            try:
                layers(manifest, spec)
            except ValueError:
                pass
            else:
                continue
        needed.append(spec)
    if not needed:
        return manifest

    previous_report = out / "source_report.json"
    previous = json.loads(previous_report.read_text()) if manifest is not None and previous_report.exists() else {}
    if manifest is not None:
        # Keep the original checksum policy, which contributes to resume identities.
        checksums = previous.get("checksums", all(manifest["sha256"].to_list()))
    source = source if source is not None else previous.get("source", "data")
    added, report = _scan_source(source, needed, checksums=checksums)
    if manifest is not None:
        names = [spec.name for spec in needed]
        recorded = manifest.filter(pl.col("property").is_in(names)).to_dicts()
        new_rows = {(r["property"], r["depth"]): r for r in added.to_dicts()}
        for row in recorded:
            if fingerprint([row]) != fingerprint([new_rows[row["property"], row["depth"]]]):
                raise ValueError(f"{row['property']}: source identity changed; inspect again")
        manifest = pl.concat([manifest.filter(~pl.col("property").is_in(names)), added])
    else:
        manifest = added
    # Check all selected sources before saving the expanded inventory or writing data.
    for spec in properties:
        verify_fresh(layers(manifest, spec))
    report["files"] = manifest.height
    seen = set(manifest.select("property", "depth").iter_rows())
    report["missing"] = [
        f"{spec.name}/{depth}"
        for spec in config.PROPERTIES.values()
        for depth in (config.DEPTH_LABELS if spec.ndim == 3 else ("profile",))
        if (spec.name, depth) not in seen
    ]
    _write_manifest(manifest, report, work_dir)
    return manifest


def read_manifest(work_dir):
    return pl.read_parquet(Path(work_dir) / config.SOURCE_VERSION / "source_manifest.parquet")


def layers(manifest, spec):
    rows = manifest.filter(pl.col("property") == spec.name).to_dicts()
    by_depth = {r["depth"]: r for r in rows}
    labels = config.DEPTH_LABELS if spec.ndim == 3 else ("profile",)
    if len(rows) != len(labels) or set(by_depth) != set(labels):
        raise ValueError(f"{spec.name}: inspect-source must record exactly every expected depth")
    return [by_depth[d] for d in labels]


def verify_fresh(rows, *, checksums=False):
    for row in rows:
        path = Path(row["path"])
        stat = path.stat()
        if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
            raise ValueError(f"source changed since inspection: {path}; inspect again")
        if checksums and row["sha256"] and checksum(path) != row["sha256"]:
            raise ValueError(f"source checksum changed: {path}")


def fingerprint(rows):
    identity = [{k: r[k] for k in ("property", "depth", "size", "mtime_ns", "sha256")} for r in rows]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
