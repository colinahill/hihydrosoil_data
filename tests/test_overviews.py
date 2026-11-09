import numpy as np
import pytest

from hihydrosoil import config, overviews, validate


@pytest.mark.parametrize("name", ["crit_wilt", "stc", "hydrologic_soil_group"])
def test_fill_aware_reducer_matches_independent_reference(name):
    spec = config.PROPERTIES[name]
    if spec.categorical:
        block = np.array(
            [
                [spec.fill, spec.flags[0], spec.flags[1]],
                [spec.flags[1], spec.flags[0], spec.fill],
                [spec.fill, spec.fill, spec.flags[-1]],
            ],
            dtype=spec.dtype,
        )
    else:
        block = np.array([[np.nan, 2, 3], [4, 6, np.nan], [np.nan, np.nan, 7]], dtype="float32")
    got = overviews.reduce_block(block, spec)
    want = validate.reference_reduce(block, spec)
    np.testing.assert_allclose(got, want, equal_nan=True)


@pytest.mark.parametrize("name", ["crit_wilt", "stc", "hydrologic_soil_group"])
def test_all_fill_and_mode_ties(name):
    spec = config.PROPERTIES[name]
    block = np.full((3, 5), spec.fill, dtype=spec.dtype)
    got = overviews.reduce_block(block, spec)
    assert np.isnan(got).all() if not spec.categorical else np.all(got == spec.fill)
    if spec.categorical:
        a, b = spec.flags[:2]
        assert overviews.reduce_block(np.array([[b, a], [a, b]], dtype=spec.dtype), spec)[0, 0] == a
