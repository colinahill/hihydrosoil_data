import shutil

import numpy as np
import pytest
import rasterio
import zarr

from hihydrosoil import catalog, config, materialize, template

from .conftest import write_sources


def test_inventory_matches_all_expected_depths(sources):
    specs, manifest = sources
    assert manifest.height == 19
    assert len(catalog.layers(manifest, specs[0])) == 6
    assert len(catalog.layers(manifest, specs[-1])) == 1


def test_stc_int16_sources_are_stored_as_int16(tiny):
    spec = config.PROPERTIES["stc"]
    # Specify the observed TIFF encoding independently of the configured expectation.
    write_sources(tiny / "data", [spec.model_copy(update={"source_dtype": "int16"})])
    catalog.inspect_source(tiny / "data", tiny / "work", [spec])
    rows = catalog.layers(catalog.read_manifest(tiny / "work"), spec)
    assert all(row["dtype"] == "int16" and row["nodata"] == -9999 for row in rows)
    g = config.GRIDS[spec.group]
    values = materialize.read_window(rows, spec, (0, g.height, 0, g.width))
    assert values.dtype == np.dtype("int16")
    with rasterio.open(rows[0]["path"]) as src:
        np.testing.assert_array_equal(values[0], src.read(1))
    assert np.all(values[:, :2, :2] == -9999)
    assert set(np.unique(values)) == {-9999, 1, 2, 3, 4, 5, 6}


def test_alfa_filenames_are_catalogued_as_alpha(tiny):
    spec = config.PROPERTIES["alpha"]
    write_sources(tiny / "data", [spec])
    for token, label in zip(config.DEPTH_TOKENS, config.DEPTH_LABELS, strict=True):
        path = tiny / "data" / f"ALFA_{token}_M_250m.tif"
        assert path.exists()
        assert catalog.identify(path) == (spec, label)
    report = catalog.inspect_source(tiny / "data", tiny / "work", [spec])
    assert report["missing"] == []
    assert len(catalog.layers(catalog.read_manifest(tiny / "work"), spec)) == 6


def test_init_corrects_legacy_alpha_metadata_without_changing_data(repository):
    spec = config.PROPERTIES["alpha"]
    session = repository.writable_session("main")
    template.init_store(session, [spec])
    root = zarr.open_group(session.store, mode="r+")
    for factor in (1, *config.OVERVIEW_FACTORS):
        group = spec.group if factor == 1 else f"{spec.group}/{factor}x"
        root[f"{group}/{spec.name}"].attrs["source_token"] = "ALPHA"
    root[spec.path][0, 0, 0] = np.float32(0.0123)
    root[spec.path].attrs["materialize_state"] = {"done": ["0-0"]}
    session.commit("legacy alpha metadata")

    session = repository.writable_session("main")
    assert template.init_store(session, [spec]) == []
    root = zarr.open_group(session.store, mode="r")
    for factor in (1, *config.OVERVIEW_FACTORS):
        group = spec.group if factor == 1 else f"{spec.group}/{factor}x"
        assert root[f"{group}/{spec.name}"].attrs["source_token"] == "ALFA"
    assert root[spec.path][0, 0, 0] == np.float32(0.0123)
    assert root[spec.path].attrs["materialize_state"] == {"done": ["0-0"]}


def test_missing_and_duplicate_depths_refused(tiny):
    spec = config.PROPERTIES["crit_wilt"]
    write_sources(tiny / "data", [spec])
    path = tiny / "data" / "CRIT-WILT_0-5cm_M_250m.tif"
    duplicate = tiny / "data" / "nested" / path.name
    duplicate.parent.mkdir()
    shutil.copy(path, duplicate)
    with pytest.raises(ValueError, match="duplicate"):
        catalog.inspect_source(tiny / "data", tiny / "work", [spec])
    duplicate.unlink()
    path.unlink()
    with pytest.raises(ValueError, match="incomplete"):
        catalog.inspect_source(tiny / "data", tiny / "work", [spec])
    report = catalog.inspect_source(tiny / "data", tiny / "work")
    assert "crit_wilt/0_5" in report["missing"]


def test_grid_drift_refused(tiny):
    spec = config.PROPERTIES["wcpf2"]
    write_sources(tiny / "data", [spec])
    path = next((tiny / "data").glob("*.tif"))
    with rasterio.open(path, "r+") as dst:
        dst.transform = dst.transform @ rasterio.Affine.translation(0.25, 0)
    with pytest.raises(ValueError, match="affine"):
        catalog.inspect_source(tiny / "data", tiny / "work", [spec])


def test_source_fingerprint_detects_change(tiny, sources):
    spec = sources[0][0]
    rows = catalog.layers(sources[1], spec)
    path = rows[0]["path"]
    with rasterio.open(path, "r+") as src:
        data = src.read(1)
        data[3, 3] += 1
        src.write(data, 1)
    with pytest.raises(ValueError, match="changed"):
        catalog.verify_fresh(rows)
