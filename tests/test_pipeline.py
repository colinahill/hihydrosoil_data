import json

import numpy as np
import pytest
import zarr
from typer.testing import CliRunner

from hihydrosoil import cli, config, grid, materialize, overviews, store, template, validate

from .conftest import write_sources


def test_native_coordinates_and_category_semantics(repository, sources):
    session = repository.readonly_session("main")
    ds = __import__("xarray").open_zarr(session.store, group="soil_properties", consolidated=False, chunks=None)
    assert list(ds.depth_interval.values) == list(config.DEPTH_LABELS)
    assert ds.stc.dtype == np.dtype("int16")
    assert ds.stc.attrs["class_codes"]["2"] == "Very fine"
    assert ds.crit_wilt.dtype == np.dtype("float32")
    assert ds.sizes["y"] == 17
    assert ds.sizes["x"] == 19
    assert zarr.open_array(session.store, path="profile_properties/hydrologic_soil_group").shape == (21, 19)
    groups = __import__("xarray").open_zarr(session.store, group="profile_properties", consolidated=False, chunks=None)
    assert groups.hydrologic_soil_group.attrs["class_codes"]["1"] == {
        "group": "A",
        "meaning": "Low runoff potential",
    }
    assert groups.hydrologic_soil_group.attrs["class_codes"]["14"] == {
        "group": "A/D",
        "meaning": "A when drained; D when undrained",
    }
    assert all(r["passed"] for r in validate.structure(session, sources[0]))


def test_additive_init_and_incompatible_encoding(repository, sources, monkeypatch):
    session = repository.writable_session("main")
    assert template.init_store(session, sources[0]) == []
    monkeypatch.setattr(config, "ENCODING", config.EncodingSpec(chunk_y=4, chunk_x=4, shard_y=8, shard_x=8))
    with pytest.raises(template.EncodingMismatch):
        template.init_store(session, sources[0])


@pytest.mark.parametrize("name", ["stc", "hydrologic_soil_group"])
def test_init_backfills_class_codes_without_changing_data_or_progress(repository, name):
    spec = config.PROPERTIES[name]
    session = repository.writable_session("main")
    root = zarr.open_group(session.store, mode="r+")
    before = {}
    expected_codes = {}
    for factor in (1, *config.OVERVIEW_FACTORS):
        group = spec.group if factor == 1 else f"{spec.group}/{factor}x"
        path = f"{group}/{name}"
        arr = root[path]
        expected_codes[path] = dict(arr.attrs["class_codes"])
        assert set(expected_codes[path]) == {str(code) for code in spec.flags}
        # Exercise both missing attributes and incorrect existing mappings.
        if factor % 4 == 0:
            arr.attrs["class_codes"] = {"1": "incorrect"}
        else:
            del arr.attrs["class_codes"]
        arr[(0,) * spec.ndim] = spec.flags[0]
        arr.attrs["materialize_state" if factor == 1 else "overview_progress"] = {"done": ["0-0"]}
        before[path] = dict(arr.attrs)
    session.commit("legacy category arrays with data and progress")
    assert not validate.structure(repository.readonly_session("main"), [spec])[0]["passed"]

    session = repository.writable_session("main")
    assert template.init_store(session, [spec]) == []
    session.commit("add class code mappings")
    session = repository.readonly_session("main")
    root = zarr.open_group(session.store, mode="r")
    for path, old_attrs in before.items():
        arr = root[path]
        assert dict(arr.attrs) == old_attrs | {"class_codes": expected_codes[path]}
        assert arr[(0,) * spec.ndim] == spec.flags[0]
        assert arr.dtype == np.dtype(spec.dtype)
    assert validate.structure(session, [spec])[0]["passed"]


def test_full_pipeline_and_idempotence(repository, sources):
    specs, manifest = sources
    for spec in specs:
        result = materialize.materialize_property(repository, spec, manifest, workers=2, commit_every=2, progress=False)
        assert result["complete"]
        assert materialize.materialize_property(repository, spec, manifest, progress=False)["processed"] == 0
        assert overviews.build_property(repository, spec, workers=2, commit_every=2, progress=False)["complete"]
        assert overviews.build_property(repository, spec, progress=False)["processed"] == 0
    session = repository.readonly_session("main")
    results = validate.structure(session, specs) + validate.contents(session, specs, manifest, samples=2, workers=2)
    assert all(r["passed"] for r in results), results
    ds = __import__("xarray").open_zarr(session.store, group="soil_properties", consolidated=False, chunks=None)
    assert ds.crit_wilt.dtype == np.dtype("float32")
    assert np.isnan(ds.crit_wilt.isel(depth_interval=0, y=0, x=0))
    assert ds.stc.dtype == np.dtype("int16")


def test_checkpoint_resume(repository, sources):
    spec, manifest = sources[0][0], sources[1]

    def interrupt(_):
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        materialize.materialize_property(
            repository, spec, manifest, commit_every=2, progress=False, after_checkpoint=interrupt
        )
    assert len(materialize.state(repository.readonly_session("main"), spec)["done"]) == 2
    result = materialize.materialize_property(repository, spec, manifest, commit_every=2, progress=False)
    assert result["processed"] == 7
    assert result["complete"]
    with pytest.raises(RuntimeError, match="interrupted"):
        overviews.build_property(repository, spec, commit_every=2, progress=False, after_checkpoint=interrupt)
    result = overviews.build_property(repository, spec, commit_every=2, progress=False)
    assert result["complete"]
    assert all(r["passed"] for r in validate.contents(repository.readonly_session("main"), [spec], manifest, samples=2))


def test_partial_native_never_releases(repository, sources):
    spec = sources[0][0]
    g = config.GRIDS[spec.group]
    bbox = (g.x_min, g.y_max - g.pixel_size, g.x_min + g.pixel_size, g.y_max)
    assert not materialize.materialize_property(repository, spec, sources[1], bbox=bbox, progress=False)["complete"]
    assert not overviews.build_property(repository, spec, progress=False)["complete"]
    with pytest.raises(ValueError, match="incomplete"):
        cli.release_gate(repository, {})


def test_overwrite_invalidates_and_rebuilds(repository, sources):
    spec = sources[0][0]
    materialize.materialize_property(repository, spec, sources[1], progress=False)
    overviews.build_property(repository, spec, progress=False)
    materialize.materialize_property(repository, spec, sources[1], overwrite=True, progress=False)
    arr = zarr.open_array(repository.readonly_session("main").store, path=spec.path)
    assert not arr.attrs["overviews_complete"]
    assert overviews.build_property(repository, spec, progress=False)["processed"] > 0


def test_local_only_and_cli_help():
    with pytest.raises(ValueError, match="local"):
        store.storage_from_uri("s3://somewhere/store")
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "materialize" in result.stdout


def test_release_requires_current_full_validation(tiny, monkeypatch):
    import icechunk

    from hihydrosoil import catalog

    spec = config.PROPERTIES["crit_wilt"]
    monkeypatch.setattr(config, "PROPERTIES", {spec.name: spec})
    write_sources(tiny / "data", [spec])
    catalog.inspect_source(tiny / "data", tiny / "work", [spec])
    local = str(tiny / "store")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(local))
    session = repo.writable_session("main")
    template.init_store(session, [spec])
    session.commit("template")
    materialize.materialize_property(repo, spec, catalog.read_manifest(tiny / "work"), progress=False)
    overviews.build_property(repo, spec, progress=False)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "--store", local, "--work-dir", str(tiny / "work"), "--samples", "2"])
    assert result.exit_code == 0, result.output
    report = json.loads((tiny / "work" / config.SOURCE_VERSION / "validation_report.json").read_text())
    assert cli.release_gate(repo, report)
    result = runner.invoke(cli.app, ["release", "--store", local, "--work-dir", str(tiny / "work")])
    assert result.exit_code == 0, result.output
    assert "hihydrosoil-2.0.0" in repo.list_tags()
    duplicate = runner.invoke(cli.app, ["release", "--store", local, "--work-dir", str(tiny / "work")])
    assert duplicate.exit_code != 0
    session = repo.writable_session("main")
    zarr.open_group(session.store, mode="r+").attrs["changed"] = True
    session.commit("new snapshot")
    with pytest.raises(ValueError, match="current snapshot"):
        cli.release_gate(repo, report)


def test_bbox_outside_grid_is_empty(tiny):
    assert (
        grid.windows(
            config.GRIDS["soil_properties"].shape, grid=config.GRIDS["soil_properties"], bbox=(-100, -10, -90, 0)
        )
        == []
    )
