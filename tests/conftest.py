from pathlib import Path

import icechunk
import numpy as np
import pytest
import rasterio

from hihydrosoil import catalog, config, materialize, template


@pytest.fixture
def tiny(monkeypatch, tmp_path):
    grids = {
        "soil_properties": config.GridSpec(width=19, height=17, x_min=10, y_max=50),
        "profile_properties": config.GridSpec(width=19, height=21, x_min=10, y_max=51),
    }
    monkeypatch.setattr(config, "GRIDS", grids)
    monkeypatch.setattr(config, "ENCODING", config.EncodingSpec(chunk_y=2, chunk_x=2, shard_y=8, shard_x=8))
    return tmp_path


def write_sources(directory: Path, specs):
    for spec in specs:
        g = config.GRIDS[spec.group]
        for depth in range(spec.depths):
            raw = np.arange(g.width * g.height).reshape(g.shape) % 500 + 1000 + depth * 10
            if spec.categorical:
                raw = np.asarray(spec.flags)[raw % len(spec.flags)]
            raw = raw.astype(spec.source_dtype)
            raw[:2, :2] = spec.source_nodata
            token = (
                f"{spec.source_token}_{config.DEPTH_TOKENS[depth]}_M_250m"
                if spec.ndim == 3
                else f"{spec.source_token}_250m"
            )
            path = directory / f"{token}.tif"
            directory.mkdir(parents=True, exist_ok=True)
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=g.height,
                width=g.width,
                count=1,
                dtype=spec.source_dtype,
                nodata=spec.source_nodata,
                crs=g.crs,
                transform=g.transform(),
            ) as dst:
                dst.write(raw, 1)


@pytest.fixture
def sources(tiny):
    specs = [config.PROPERTIES[n] for n in ("crit_wilt", "wcpf2", "stc", "hydrologic_soil_group")]
    write_sources(tiny / "data", specs)
    catalog.inspect_source(tiny / "data", tiny / "work", specs)
    return specs, catalog.read_manifest(tiny / "work")


@pytest.fixture
def repository(tiny, sources):
    repo = icechunk.Repository.create(icechunk.in_memory_storage())
    session = repo.writable_session("main")
    template.init_store(session, sources[0])
    materialize.commit_session(session, "fixture template")
    return repo
