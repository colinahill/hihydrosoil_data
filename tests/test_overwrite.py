import numpy as np
import zarr

from hihydrosoil import materialize, overviews


def test_overwrite_clears_fill_and_stale_overview(repository, sources):
    spec = sources[0][0]
    materialize.materialize_property(repository, spec, sources[1], progress=False)
    session = repository.writable_session("main")
    arr = zarr.open_array(session.store, path=spec.path, mode="r+")
    # Simulate incorrect stored data in a source nodata region.
    arr[:, :2, :2] = np.float32(0.99)
    session.commit("inject incorrect pixels")
    overviews.build_property(repository, spec, progress=False)
    coarse = zarr.open_array(repository.readonly_session("main").store, path=f"{spec.group}/2x/{spec.name}")
    assert coarse[0, 0, 0] == np.float32(0.99)
    materialize.materialize_property(repository, spec, sources[1], overwrite=True, progress=False)
    overviews.build_property(repository, spec, progress=False)
    session = repository.readonly_session("main")
    arr = zarr.open_array(session.store, path=spec.path)
    coarse = zarr.open_array(session.store, path=f"{spec.group}/2x/{spec.name}")
    assert np.isnan(arr[:, :2, :2]).all()
    assert np.isnan(coarse[:, 0, 0]).all()
