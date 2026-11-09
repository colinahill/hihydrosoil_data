import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hihydrosoil import catalog, cli, config, materialize

from .conftest import write_sources


@pytest.mark.parametrize("checksums", [True, False])
def test_materialize_initializes_manifest_and_resumes(tiny, repository, monkeypatch, checksums):
    spec = config.PROPERTIES["crit_wilt"]
    work = tiny / "automatic"
    monkeypatch.setattr(cli.store, "open_repo", lambda _: repository)
    args = [
        "materialize",
        "--source",
        str(tiny / "data"),
        "--work-dir",
        str(work),
        "--properties",
        spec.name,
    ]
    if not checksums:
        args.append("--no-checksums")
    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["complete"]
    manifest = catalog.read_manifest(work)
    assert manifest.height == 6
    assert all(bool(row["sha256"]) == checksums for row in catalog.layers(manifest, spec))
    assert materialize.state(repository.readonly_session("main"), spec)["complete"]

    def unexpected_scan(*args, **kwargs):
        pytest.fail("a complete manifest must not be scanned or hashed again")

    monkeypatch.setattr(catalog, "_scan_source", unexpected_scan)
    snapshot = repository.readonly_session("main").snapshot_id
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["processed"] == 0
    assert repository.readonly_session("main").snapshot_id == snapshot


@pytest.mark.parametrize("checksums", [True, False])
def test_manifest_extension_preserves_fingerprints_and_checksum_policy(tiny, repository, monkeypatch, checksums):
    old, added = (config.PROPERTIES[name] for name in ("crit_wilt", "wcpf2"))
    work = tiny / "work"
    catalog.inspect_source(tiny / "data", work, [old], checksums=checksums)
    before = catalog.layers(catalog.read_manifest(work), old)
    materialize.materialize_property(repository, old, catalog.read_manifest(work), progress=False)
    original_checksum = catalog.checksum
    hashed = []

    def track_checksum(path):
        hashed.append(path.name)
        assert catalog.identify(path)[0] == added
        return original_checksum(path)

    monkeypatch.setattr(catalog, "checksum", track_checksum)
    # The source and checksum policy are taken from the previous inspection.
    manifest = catalog.ensure_manifest(work, [old, added], checksums=not checksums)
    assert catalog.layers(manifest, old) == before
    assert manifest.height == 12
    assert len(hashed) == (6 if checksums else 0)
    assert all(bool(row["sha256"]) == checksums for row in catalog.layers(manifest, added))
    report = json.loads((work / config.SOURCE_VERSION / "source_report.json").read_text())
    assert report["files"] == 12
    assert report["checksums"] == checksums
    assert materialize.materialize_property(repository, old, manifest, progress=False)["processed"] == 0
    assert materialize.materialize_property(repository, added, manifest, progress=False)["complete"]


def test_partial_inventory_is_completed_without_changing_recorded_layers(tiny):
    spec = config.PROPERTIES["field_crit"]
    write_sources(tiny / "data", [spec])
    path = tiny / "data" / "FIELD-CRIT_0-5cm_M_250m.tif"
    held = path.with_suffix(".held")
    path.rename(held)
    catalog.inspect_source(tiny / "data", tiny / "work", checksums=False)
    before = catalog.read_manifest(tiny / "work").to_dicts()
    held.rename(path)
    manifest = catalog.ensure_manifest(tiny / "work", [spec])
    assert len(catalog.layers(manifest, spec)) == 6
    assert all(row in manifest.to_dicts() for row in before)


@pytest.mark.parametrize("failure", ["missing_depth", "changed_source"])
def test_failed_preflight_preserves_manifest_and_never_opens_store(tiny, repository, monkeypatch, failure):
    old = config.PROPERTIES["crit_wilt"]
    added = config.PROPERTIES["field_crit"]
    write_sources(tiny / "data", [added])
    catalog.inspect_source(tiny / "data", tiny / "work", [old], checksums=False)
    out = tiny / "work" / config.SOURCE_VERSION
    before = {name: (out / name).read_bytes() for name in ("source_manifest.parquet", "source_report.json")}
    if failure == "missing_depth":
        (tiny / "data" / "FIELD-CRIT_0-5cm_M_250m.tif").unlink()
        expected = "incomplete"
    else:
        path = Path(catalog.layers(catalog.read_manifest(tiny / "work"), old)[0]["path"])
        with path.open("ab") as stream:
            stream.write(b"changed")
        expected = "source changed"

    def unexpected_open(*args, **kwargs):
        pytest.fail("invalid sources must fail before opening the store")

    monkeypatch.setattr(cli.store, "open_repo", unexpected_open)
    result = CliRunner().invoke(
        cli.app,
        [
            "materialize",
            "--source",
            str(tiny / "data"),
            "--work-dir",
            str(tiny / "work"),
            "--properties",
            f"{old.name},{added.name}",
        ],
    )
    assert isinstance(result.exception, ValueError)
    assert expected in str(result.exception)
    assert {name: (out / name).read_bytes() for name in before} == before
