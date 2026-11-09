"""Shard-aligned native writes with bounded workers and durable resume ledgers."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import zarr
from rasterio.windows import Window
from tqdm import tqdm

from . import catalog, config, grid, store, template


def retry_transient(action, attempts=5):
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:
            if store.is_credentials_failure(exc) or not isinstance(exc, (OSError, icechunk_storage_error())):
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(min(2**attempt, 8))


def icechunk_storage_error():
    import icechunk

    return icechunk.StorageError


def decode_values(raw, spec):
    if spec.categorical:
        valid = raw != spec.source_nodata
        if np.any(valid & ~np.isin(raw, spec.flags)):
            raise ValueError(f"{spec.name}: unexpected category codes: {np.unique(raw[valid]).tolist()}")
        return raw.astype(spec.dtype, copy=False)
    values = raw.astype("float32") / np.float32(10000)
    values[raw == spec.source_nodata] = np.nan
    return values


def all_fill(values, spec):
    return bool(np.all(values == spec.fill)) if spec.categorical else bool(np.isnan(values).all())


def read_window(rows, spec, window):
    y0, y1, x0, x1 = window
    blocks = []
    # Handles live only for this task; rasterio DatasetReader is not thread-safe.
    for row in rows:
        with rasterio.open(row["path"]) as src:
            grid.verify_source(src, spec)
            raw = src.read(1, window=Window(x0, y0, x1 - x0, y1 - y0))
        blocks.append(decode_values(raw, spec))
    return np.stack(blocks) if spec.ndim == 3 else blocks[0]


def state(session, spec):
    return dict(zarr.open_array(session.store, path=spec.path, mode="r").attrs.get("materialize_state", {}))


def commit_session(session, message):
    if not session.has_uncommitted_changes:
        return session.snapshot_id
    return retry_transient(lambda: store.commit_with_rebase(session, message))


def materialize_property(
    repo, spec, manifest, *, bbox=None, workers=4, commit_every=8, overwrite=False, progress=True, after_checkpoint=None
):
    if workers < 1 or commit_every < 1:
        raise ValueError("workers and commit-every must be positive")
    rows = catalog.layers(manifest, spec)
    catalog.verify_fresh(rows)
    identity = catalog.fingerprint(rows)
    session = repo.writable_session("main")
    arr = zarr.open_array(session.store, path=spec.path, mode="r+")
    template.verify_array(arr, spec)
    previous = state(session, spec)
    if previous and previous.get("source_fingerprint") != identity:
        raise ValueError(f"{spec.name}: source identity changed; use a new dataset version/store")
    all_windows = grid.windows(config.GRIDS[spec.group].shape)
    selected = grid.windows(config.GRIDS[spec.group].shape, bbox=bbox, grid=config.GRIDS[spec.group])
    if not selected:
        raise ValueError("bbox does not intersect the source grid")
    done = set(previous.get("done", []))
    active = set(previous.get("active", []))
    coverage = set(previous.get("overview_coverage", []))
    pending = [w for w in selected if overwrite or grid.window_key(w) not in done]
    if not pending:
        return {"property": spec.name, "processed": 0, "complete": previous.get("complete", False)}
    # Invalidate completion before modifying native data, even if a later task fails.
    arr.attrs["overviews_complete"] = False
    arr.attrs["materialize_state"] = {
        "source_fingerprint": identity,
        "done": sorted(done),
        "complete": False,
        "revision": previous.get("revision", 0) + 1,
        "active": sorted(active),
        "overview_coverage": sorted(coverage),
    }
    if overwrite:
        done.difference_update(grid.window_key(w) for w in selected)
        active.difference_update(grid.window_key(w) for w in selected)
        record = dict(arr.attrs["materialize_state"])
        record["done"] = sorted(done)
        record["active"] = sorted(active)
        arr.attrs["materialize_state"] = record
    commit_session(session, f"invalidate {spec.name} before native writes")
    revision = previous.get("revision", 0) + 1
    processed = written = empty = 0
    bar = tqdm(total=len(pending), desc=f"materialize {spec.name}", unit="shard", disable=not progress)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(pending), commit_every):
                batch = pending[offset : offset + commit_every]
                session = repo.writable_session("main")
                arr = zarr.open_array(session.store, path=spec.path, mode="r+")

                def process(window, array=arr):
                    data = read_window(rows, spec, window)
                    y0, y1, x0, x1 = window
                    sel = (
                        (slice(None), slice(y0, y1), slice(x0, x1))
                        if spec.ndim == 3
                        else (slice(y0, y1), slice(x0, x1))
                    )
                    is_empty = all_fill(data, spec)
                    # Overwrites must clear previously nonempty shards too.
                    if not is_empty or overwrite:
                        retry_transient(lambda: array.__setitem__(sel, data))
                    return is_empty

                counts = list(pool.map(process, batch))
                catalog.verify_fresh(rows)
                done.update(grid.window_key(w) for w in batch)
                for window, is_empty in zip(batch, counts, strict=True):
                    if not is_empty:
                        active.add(grid.window_key(window))
                        coverage.add(grid.window_key(window))
                complete = len(done) == len(all_windows)
                arr.attrs["materialize_state"] = {
                    "source_fingerprint": identity,
                    "done": sorted(done),
                    "complete": complete,
                    "revision": revision,
                    "active": sorted(active),
                    "overview_coverage": sorted(coverage),
                    "source_layers": [{k: r[k] for k in ("depth", "sha256", "size")} for r in rows],
                }
                commit_session(session, f"materialize {spec.name}: {len(done)}/{len(all_windows)} shards")
                processed += len(batch)
                empty += sum(counts)
                written += len(batch) - sum(counts)
                bar.update(len(batch))
                if after_checkpoint:
                    after_checkpoint(processed)
    finally:
        bar.close()
    return {"property": spec.name, "processed": processed, "written": written, "all_fill": empty, "complete": complete}
