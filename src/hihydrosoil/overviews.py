"""Windowed, resumable stride-two pyramids. Native arrays retain their location."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import zarr
from topozarr.engine import block_reduce
from tqdm import tqdm

from . import config, materialize, template


def reduce_block(block, spec):
    """Reduce two spatial dimensions; pad odd edges, preserving all source pixels."""
    h, w = block.shape[-2:]
    padded = np.full(((h + 1) // 2 * 2, (w + 1) // 2 * 2), spec.fill, dtype=spec.dtype)
    padded[:h, :w] = block
    if spec.categorical:
        # topozarr 0.1.6 has no mode kernel. Count this small, known codebook
        # directly, excluding fill; ascending order resolves ties to the lowest code.
        blocks = padded.reshape(padded.shape[0] // 2, 2, padded.shape[1] // 2, 2)
        result = np.full((padded.shape[0] // 2, padded.shape[1] // 2), spec.fill, dtype=spec.dtype)
        best = np.zeros(result.shape, dtype="uint8")
        for code in sorted(spec.flags):
            count = np.sum(blocks == code, axis=(1, 3))
            replace = count > best
            result[replace] = code
            best[replace] = count[replace]
        return result
    return block_reduce(np.ascontiguousarray(padded), (2, 2), "mean", spec.fill, True).astype(spec.dtype)


def regions(spec, done, factor):
    """Destination shards that intersect completed native shards, once per depth."""
    sy, sx = config.ENCODING.shard_y, config.ENCODING.shard_x
    g = config.GRIDS[spec.group]
    h, w = g.shape_at(factor)
    keys = set()
    for key in done:
        r, c = (int(v) for v in key.split("-"))
        y0, y1 = r * sy // factor, (min((r + 1) * sy, g.height) + factor - 1) // factor
        x0, x1 = c * sx // factor, (min((c + 1) * sx, g.width) + factor - 1) // factor
        for y in range(y0 // sy * sy, y1, sy):
            for x in range(x0 // sx * sx, x1, sx):
                keys.add((y, min(y + sy, h), x, min(x + sx, w)))
    return [(d, *window) for d in range(spec.depths) for window in sorted(keys)]


def build_property(repo, spec, *, workers=4, commit_every=8, rebuild=False, progress=True, after_checkpoint=None):
    if workers < 1 or commit_every < 1:
        raise ValueError("workers and commit-every must be positive")
    session = repo.writable_session("main")
    native = zarr.open_array(session.store, path=spec.path, mode="r+")
    ledger = materialize.state(session, spec)
    if not ledger.get("done"):
        raise ValueError(f"{spec.name}: no native shards have been materialized")
    scope = hashlib.sha256(
        json.dumps({"ledger": ledger, "factors": config.OVERVIEW_FACTORS}, sort_keys=True).encode()
    ).hexdigest()
    if native.attrs.get("overviews_complete") and native.attrs.get("overview_scope") == scope and not rebuild:
        return {"property": spec.name, "processed": 0, "complete": True}
    native.attrs["overviews_complete"] = False
    materialize.commit_session(session, f"invalidate {spec.name} overview completion")
    count = 0
    for factor in config.OVERVIEW_FACTORS:
        source_path = spec.group if factor == 2 else f"{spec.group}/{factor // 2}x"
        target_path = f"{spec.group}/{factor}x/{spec.name}"
        session = repo.writable_session("main")
        dst = zarr.open_array(session.store, path=target_path, mode="r+")
        template.verify_array(dst, spec, factor)
        previous = dict(dst.attrs.get("overview_progress", {}))
        start = previous.get("done", 0) if previous.get("scope") == scope and not rebuild else 0
        tasks = regions(spec, ledger.get("overview_coverage", ledger["done"]), factor)
        bar = tqdm(
            total=len(tasks), initial=start, desc=f"overviews {spec.name}/{factor}x", unit="shard", disable=not progress
        )
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for offset in range(start, len(tasks), commit_every):
                    batch = tasks[offset : offset + commit_every]
                    session = repo.writable_session("main")
                    src = zarr.open_array(session.store, path=f"{source_path}/{spec.name}", mode="r")
                    dst = zarr.open_array(session.store, path=target_path, mode="r+")

                    def process(task, source=src, destination=dst):
                        depth, y0, y1, x0, x1 = task
                        spatial = (
                            slice(y0 * 2, min(y1 * 2, source.shape[-2])),
                            slice(x0 * 2, min(x1 * 2, source.shape[-1])),
                        )
                        selection = (depth, *spatial) if spec.ndim == 3 else spatial
                        block = materialize.retry_transient(lambda: source[selection])
                        reduced = reduce_block(block, spec)
                        target = (
                            (depth, slice(y0, y1), slice(x0, x1)) if spec.ndim == 3 else (slice(y0, y1), slice(x0, x1))
                        )
                        # Write fill too: rebuilding must remove stale overview values.
                        materialize.retry_transient(lambda: destination.__setitem__(target, reduced))

                    list(pool.map(process, batch))
                    current = materialize.state(session, spec)
                    if current != ledger:
                        raise ValueError(f"{spec.name}: native data changed while building overviews; rerun")
                    dst.attrs["overview_progress"] = {"scope": scope, "done": offset + len(batch), "total": len(tasks)}
                    materialize.commit_session(
                        session, f"overviews {spec.name}/{factor}x: {offset + len(batch)}/{len(tasks)}"
                    )
                    count += len(batch)
                    bar.update(len(batch))
                    if after_checkpoint:
                        after_checkpoint(count)
        finally:
            bar.close()
    session = repo.writable_session("main")
    if materialize.state(session, spec) != ledger:
        raise ValueError(f"{spec.name}: native data changed; rerun overviews")
    native = zarr.open_array(session.store, path=spec.path, mode="r+")
    native.attrs.update(
        overviews_complete=bool(ledger.get("complete")),
        overview_scope=scope,
        overviews_built_factors=list(config.OVERVIEW_FACTORS),
    )
    materialize.commit_session(session, f"complete overview scope for {spec.name}")
    return {"property": spec.name, "processed": count, "complete": bool(ledger.get("complete"))}
