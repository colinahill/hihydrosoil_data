"""Structural gates, source comparisons and independent overview reductions."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import zarr
from rasterio.windows import Window

from . import catalog, config, grid, materialize, metadata, template


def structure(session, properties):
    results = []
    for spec in properties:
        try:
            for factor in (1, *config.OVERVIEW_FACTORS):
                path = spec.group if factor == 1 else f"{spec.group}/{factor}x"
                gp = zarr.open_group(session.store, path=path, mode="r")
                template.verify_array(gp[spec.name], spec, factor)
                want_x, want_y = config.GRIDS[spec.group].coords(factor)
                if not np.array_equal(gp["x"][:], want_x) or not np.array_equal(gp["y"][:], want_y):
                    raise ValueError(f"{path}: coordinate mismatch")
                from pyproj import CRS

                if CRS.from_wkt(gp["spatial_ref"].attrs["crs_wkt"]) != CRS(config.GRIDS[spec.group].crs):
                    raise ValueError(f"{path}: CRS mismatch")
                attrs = dict(gp[spec.name].attrs)
                if attrs.get("units") != spec.units or attrs.get("source_token") != spec.source_token:
                    raise ValueError(f"{path}/{spec.name}: units/source metadata mismatch")
                if (
                    spec.categorical
                    and attrs.get("class_codes") != metadata.property_attrs(spec, factor)["class_codes"]
                ):
                    raise ValueError(f"{path}/{spec.name}: class_codes metadata mismatch; run init-store")
                if not spec.categorical and ("scale_factor" in attrs or "_FillValue" in attrs):
                    raise ValueError("decoded float arrays must not have CF packing attributes")
            results.append({"check": f"{spec.name}: structure", "passed": True})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            results.append({"check": f"{spec.name}: structure", "passed": False, "detail": str(exc)})
    return results


def reference_reduce(block, spec):
    """Deliberately independent of the Rust reduction, including partial edges."""
    h, w = block.shape
    result = np.full(((h + 1) // 2, (w + 1) // 2), spec.fill, dtype=spec.dtype)
    for y in range(result.shape[0]):
        for x in range(result.shape[1]):
            values = block[y * 2 : min(y * 2 + 2, h), x * 2 : min(x * 2 + 2, w)].ravel()
            valid = values[values != spec.fill] if spec.categorical else values[np.isfinite(values)]
            if valid.size:
                if spec.categorical:
                    codes, counts = np.unique(valid, return_counts=True)
                    result[y, x] = codes[counts.argmax()]
                else:
                    result[y, x] = valid.astype("float64").mean()
    return result


def check_source_window(session, spec, rows, window, depth):
    arr = zarr.open_array(session.store, path=spec.path, mode="r")
    y0, y1, x0, x1 = window
    with rasterio.open(rows[depth]["path"]) as src:
        raw = src.read(1, window=Window(x0, y0, x1 - x0, y1 - y0))
    sel = (depth, slice(y0, y1), slice(x0, x1)) if spec.ndim == 3 else (slice(y0, y1), slice(x0, x1))
    got = arr[sel]
    mask = raw != spec.source_nodata
    if not np.any(mask):
        return None
    if spec.categorical:
        passed = np.array_equal(got, raw)
        metrics = {"comparison": "exact source category codes"}
    else:
        expected = raw.astype("float32") / np.float32(10000)
        expected[~mask] = np.nan
        passed = np.array_equal(got, expected, equal_nan=True)
        error = np.abs(got[mask].astype("float64") - raw[mask].astype("float64") / 10000)
        recovered = np.rint(got[mask].astype("float64") * 10000)
        metrics = {
            "comparison": "exact declared float32 conversion",
            "max_absolute_conversion_error": float(error.max()),
            "integer_roundtrip_differences": int(np.count_nonzero(recovered != raw[mask])),
        }
    return {
        "check": f"{spec.name}: source pixels at depth {depth}, window {window}",
        "passed": bool(passed),
        "valid_pixels": int(mask.sum()),
        **metrics,
    }


def contents(session, properties, manifest, *, samples=8, workers=4, seed=0):
    if samples < 1 or workers < 1:
        raise ValueError("samples and workers must be positive")
    rng = random.Random(seed)
    results = []
    for spec in properties:
        rows = catalog.layers(manifest, spec)
        catalog.verify_fresh(rows)
        ledger = materialize.state(session, spec)
        done = set(ledger.get("active", ledger.get("done", [])))
        candidates = [w for w in grid.windows(config.GRIDS[spec.group].shape) if grid.window_key(w) in done]
        if not candidates:
            results.append({"check": f"{spec.name}: source sampling", "passed": False, "detail": "no completed shards"})
            continue
        rng.shuffle(candidates)
        # Bounded candidates, but never report success over zero valid pixels.
        picked = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, min(len(candidates), max(samples * 8, 32)), workers):
                batch = candidates[start : start + workers]
                args = []
                for i, window in enumerate(batch):
                    y0, y1, x0, x1 = window
                    max_y, max_x = min(64, y1 - y0), min(64, x1 - x0)
                    # Source-tile blocks are spatially correlated; vary the position within a shard.
                    y = rng.randint(y0, y1 - max_y)
                    x = rng.randint(x0, x1 - max_x)
                    args.append(((y, y + max_y, x, x + max_x), (start + i) % spec.depths))
                found = list(
                    pool.map(lambda p, spec=spec, rows=rows: check_source_window(session, spec, rows, p[0], p[1]), args)
                )
                picked.extend(r for r in found if r is not None)
                if len(picked) >= samples:
                    break
        results.extend(picked[:samples])
        if len(picked) < samples:
            results.append(
                {
                    "check": f"{spec.name}: source sampling",
                    "passed": False,
                    "detail": f"only {len(picked)} nonempty windows found; requested {samples}",
                }
            )
        for factor in config.OVERVIEW_FACTORS:
            gp = spec.group if factor == 2 else f"{spec.group}/{factor // 2}x"
            src = zarr.open_array(session.store, path=f"{gp}/{spec.name}", mode="r")
            dst = zarr.open_array(session.store, path=f"{spec.group}/{factor}x/{spec.name}", mode="r")
            window = candidates[0]
            # Test a window including meaningful materialized coverage and, separately, the ragged edge.
            points = [(window[0] // factor, window[2] // factor), (dst.shape[-2] - 1, dst.shape[-1] - 1)]
            for y, x in points:
                y1, x1 = min(y + 8, dst.shape[-2]), min(x + 8, dst.shape[-1])
                spatial = (slice(y * 2, min(y1 * 2, src.shape[-2])), slice(x * 2, min(x1 * 2, src.shape[-1])))
                sel = (0, *spatial) if spec.ndim == 3 else spatial
                want = reference_reduce(src[sel], spec)
                sel_dst = (0, slice(y, y1), slice(x, x1)) if spec.ndim == 3 else (slice(y, y1), slice(x, x1))
                got = dst[sel_dst]
                ok = (
                    np.array_equal(got, want)
                    if spec.categorical
                    else np.allclose(got, want, rtol=2e-6, atol=1e-7, equal_nan=True)
                )
                results.append({"check": f"{spec.name}: overview {factor}x at ({y},{x})", "passed": bool(ok)})
    return results


def validation_identity(session, properties):
    return {
        spec.name: {
            "native": materialize.state(session, spec),
            "overview_scope": zarr.open_array(session.store, path=spec.path, mode="r").attrs.get("overview_scope"),
        }
        for spec in properties
    }
