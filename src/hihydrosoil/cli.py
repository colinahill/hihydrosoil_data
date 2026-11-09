"""Typer commands own sessions/commits; computational modules remain reusable."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
import zarr

from . import catalog, config, grid, materialize, overviews, remote, store, template, validate

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
DEFAULT_STORE = f"./hihydrosoil_store_local/v{config.DATASET_VERSION}.icechunk"


@app.callback()
def main(verbose: bool = False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")


def target(local, account, credentials_file):
    if account and local != DEFAULT_STORE:
        raise ValueError("choose either --store or --source-coop-account")
    return (
        store.source_coop_storage(account, credentials_file=credentials_file)
        if account
        else store.storage_from_uri(local)
    )


@app.command()
def inspect_source(
    source: Path = Path("data"),
    work_dir: Path = Path("work"),
    properties: str | None = None,
    no_checksums: bool = False,
):
    """Verify extracted local TIFFs and write a manifest; missing downloads are reported."""
    selected = config.select_properties(properties) if properties else None
    report = catalog.inspect_source(source, work_dir, selected, checksums=not no_checksums)
    print(json.dumps(report, indent=2))


@app.command()
def init_store(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    properties: str | None = None,
):
    """Create the versioned store and additive empty native/overview arrays."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file), create=True)
    session = repo.writable_session("main")
    created = template.init_store(session, config.select_properties(properties))
    if session.has_uncommitted_changes:
        materialize.commit_session(session, "initialize HiHydroSoil structure")
    print(json.dumps({"created": created}))


@app.command("materialize")
def materialize_cmd(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    properties: str | None = None,
    work_dir: Path = Path("work"),
    source: Annotated[
        Path | None, typer.Option(help="Local TIFF directory; defaults to the inspected source or data.")
    ] = None,
    no_checksums: bool = False,
    bbox: str | None = None,
    workers: int = 4,
    commit_every: int = 8,
    overwrite: bool = False,
):
    """Inspect missing sources, fill selected properties and resume completed shards."""
    selected = config.select_properties(properties)
    manifest = catalog.ensure_manifest(work_dir, selected, source=source, checksums=not no_checksums)
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    for spec in selected:
        print(
            json.dumps(
                materialize.materialize_property(
                    repo,
                    spec,
                    manifest,
                    bbox=grid.parse_bbox(bbox),
                    workers=workers,
                    commit_every=commit_every,
                    overwrite=overwrite,
                )
            )
        )


@app.command("overviews")
def overviews_cmd(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    properties: str | None = None,
    workers: int = 4,
    commit_every: int = 8,
    rebuild: bool = False,
):
    """Build the 2x-256x fill-aware pyramid in checkpointed destination shards."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    for spec in config.select_properties(properties):
        print(
            json.dumps(
                overviews.build_property(repo, spec, workers=workers, commit_every=commit_every, rebuild=rebuild)
            )
        )


@app.command()
def status(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
):
    """Show native and overview completion, including checkpoint progress."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    session = repo.readonly_session("main")
    rows = []
    for spec in config.PROPERTIES.values():
        try:
            arr = zarr.open_array(session.store, path=spec.path, mode="r")
        except (FileNotFoundError, KeyError):
            rows.append((spec.path, "-", "Not initialized", "-"))
            continue

        ledger = materialize.state(session, spec)
        done = len(ledger.get("done", []))
        total = len(grid.windows(config.GRIDS[spec.group].shape))
        native = "Complete" if ledger.get("complete", False) else "In progress" if done else "Not started"
        progress = []
        for f in config.OVERVIEW_FACTORS:
            try:
                a = zarr.open_array(session.store, path=f"{spec.group}/{f}x/{spec.name}", mode="r")
            except (FileNotFoundError, KeyError):
                progress.append(f"{f}x: not initialized")
                continue
            p = a.attrs.get("overview_progress")
            if p and p["done"] < p["total"]:
                progress.append(f"{f}x: {p['done']:,}/{p['total']:,} regions")
        overview = "Complete" if arr.attrs.get("overviews_complete", False) else "; ".join(progress) or "Pending"
        rows.append((spec.path, f"{done:,}/{total:,}", native, overview))

    headers = ("Property", "Native shards", "Native", "Overviews")
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    row_format = "  ".join("{:" + (">" if i == 1 else "<") + str(width) + "}" for i, width in enumerate(widths))
    print(row_format.format(*headers).rstrip())
    print(row_format.format(*("-" * width for width in widths)))
    for row in rows:
        print(row_format.format(*row).rstrip())


@app.command("validate")
def validate_cmd(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    properties: str | None = None,
    work_dir: Path = Path("work"),
    samples: int = 8,
    workers: int = 4,
    seed: int = 0,
    structure_only: bool = False,
):
    """Check structure, native source windows and independently reduced overviews."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    selected = config.select_properties(properties)
    snapshot = repo.lookup_branch("main")
    session = repo.readonly_session(snapshot_id=snapshot)
    results = validate.structure(session, selected)
    if not structure_only and all(r["passed"] for r in results):
        results.extend(
            validate.contents(
                session, selected, catalog.read_manifest(work_dir), samples=samples, workers=workers, seed=seed
            )
        )
    report = {
        "snapshot": snapshot,
        "dataset_version": config.DATASET_VERSION,
        "source_version": config.SOURCE_VERSION,
        "structure_only": structure_only,
        "identity": validate.validation_identity(session, selected),
        "results": results,
        "passed": all(r["passed"] for r in results),
    }
    out = work_dir / config.SOURCE_VERSION
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "validation_report.json.tmp"
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(out / "validation_report.json")
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['check']}: {r.get('detail', '')}")
    if not report["passed"]:
        raise typer.Exit(1)


def release_gate(repo, report):
    session = repo.readonly_session("main")
    selected = list(config.PROPERTIES.values())
    missing = []
    for spec in selected:
        try:
            arr = zarr.open_array(session.store, path=spec.path, mode="r")
            if not materialize.state(session, spec).get("complete") or not arr.attrs.get("overviews_complete"):
                missing.append(spec.name)
        except (FileNotFoundError, KeyError):
            missing.append(spec.name)
    if missing:
        raise ValueError(f"refusing release; incomplete native/overviews: {', '.join(missing)}")
    if (
        not report.get("passed")
        or report.get("structure_only")
        or report.get("dataset_version") != config.DATASET_VERSION
        or report.get("snapshot") != repo.lookup_branch("main")
        or report.get("identity") != validate.validation_identity(session, selected)
    ):
        raise ValueError("refusing release; run full validate for all properties on the current snapshot")
    return session


@app.command()
def release(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    work_dir: Path = Path("work"),
    suffix: str = "",
):
    """Gate complete, validated data and create an immutable release tag."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    report = json.loads((work_dir / config.SOURCE_VERSION / "validation_report.json").read_text())
    session = release_gate(repo, report)
    tag = f"hihydrosoil-{config.SOURCE_VERSION}{suffix}"
    repo.create_tag(tag, snapshot_id=session.snapshot_id)
    tagged = repo.readonly_session(tag=tag)
    if not all(r["passed"] for r in validate.structure(tagged, list(config.PROPERTIES.values()))):
        raise typer.Exit(1)
    print(f"released {tag} at {session.snapshot_id}")


@app.command()
def info(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
):
    """Show the store tree and tags."""
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    print(zarr.open_group(repo.readonly_session("main").store, mode="r").tree())
    print("tags:", repo.list_tags())


@app.command()
def garbage_collect(
    store_path: str = typer.Option(DEFAULT_STORE, "--store"),
    source_coop_account: str | None = None,
    credentials_file: str | None = None,
    older_than_hours: int = 24,
):
    """Reclaim unreferenced objects. Run only while no writers are active."""
    from datetime import UTC, datetime, timedelta

    if older_than_hours < 1:
        raise ValueError("older-than-hours must be positive")
    repo = store.open_repo(target(store_path, source_coop_account, credentials_file))
    print(repo.garbage_collect(datetime.now(UTC) - timedelta(hours=older_than_hours)))


@app.command()
def publish_readme(source_coop_account: str = "chill", credentials_file: str | None = None):
    """Upload product/README.md to the Source Cooperative product root."""
    remote.upload_readme(source_coop_account, credentials_file=credentials_file)


@app.command()
def upload_audit(
    source_coop_account: str = "chill", credentials_file: str | None = None, work_dir: Path = Path("work")
):
    """Upload source and validation reports to audit/{source_version}/."""
    files = sorted((work_dir / config.SOURCE_VERSION).glob("*"))
    files = [p for p in files if p.is_file() and p.suffix in {".json", ".parquet"}]
    if not files:
        raise ValueError("no audit artifacts found")
    remote.upload_audit(source_coop_account, files, config.SOURCE_VERSION, credentials_file=credentials_file)


@app.command()
def clean_remote_store(source_coop_account: str = "chill", credentials_file: str | None = None, workers: int = 4):
    """DESTRUCTIVE: remove versioned store objects, requiring two confirmations."""
    import sys

    if not sys.stdin.isatty():
        raise ValueError("remote cleanup requires an interactive terminal")
    keys, size = remote.store_keys(source_coop_account, credentials_file=credentials_file)
    typer.confirm(
        f"Delete {len(keys)} objects ({size:,} bytes) from {source_coop_account}/hihydrosoil/{store.STORE_SUBPATH}?",
        abort=True,
    )
    typer.confirm("Permanently remove all data, snapshots and tags at that prefix?", abort=True)
    remote.delete_keys(source_coop_account, keys, credentials_file=credentials_file, workers=workers)


@app.command()
def show_config():
    """Print the frozen grids, encoding and property inventory."""
    print(
        json.dumps(
            {
                "source_version": config.SOURCE_VERSION,
                "dataset_version": config.DATASET_VERSION,
                "grids": {k: v.model_dump() for k, v in config.GRIDS.items()},
                "encoding": config.ENCODING.model_dump(),
                "properties": {k: v.model_dump() for k, v in config.PROPERTIES.items()},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
