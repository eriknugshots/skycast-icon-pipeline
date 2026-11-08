import numpy as np
import pytest

from globalfeed.latlon import to_feed
from pipeline.squares import DEG, NX, NY

STEP = 0.25


def f(lat, lon):
    return 50 + 40 * np.sin(np.radians(lat)) * np.cos(np.radians(lon))


def source(layout, fn=f):
    """Flat lat/lon/value arrays as eccodes returns them: lat 90 -> -90, lon west to east."""
    lats = 90 - STEP * np.arange(721)
    lons = (STEP * np.arange(1440)) if layout == "gfs" else (-180 + STEP * np.arange(1440))
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return la.ravel(), lo.ravel(), fn(la, lo).ravel()


def node(out, lat, lon):
    return out[round((lat + 90) / DEG), round((lon + 180) / DEG)]


LAYOUTS = ["gfs", "aifs"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_shape_and_dtype(layout):
    out = to_feed(*source(layout))
    assert out.shape == (NY, NX)
    assert out.dtype == np.uint8


@pytest.mark.parametrize("layout", LAYOUTS)
def test_on_point_nodes_copy_source(layout):
    out = to_feed(*source(layout))
    assert node(out, 10, 20) == np.rint(f(10, 20))
    assert node(out, -90, -180) == np.rint(f(-90, -180))
    assert node(out, 90, 0) == np.rint(f(90, 0))


@pytest.mark.parametrize("layout", LAYOUTS)
def test_midpoints_average(layout):
    out = to_feed(*source(layout))
    assert node(out, 10.125, 20) == np.rint((f(10, 20) + f(10.25, 20)) / 2)
    assert node(out, 10, 20.125) == np.rint((f(10, 20) + f(10, 20.25)) / 2)
    four = [f(10, 20), f(10.25, 20), f(10, 20.25), f(10.25, 20.25)]
    assert node(out, 10.125, 20.125) == np.rint(np.mean(four))


def test_dateline_wraps_both_layouts():
    outs = [to_feed(*source(layout)) for layout in LAYOUTS]
    assert np.array_equal(outs[0], outs[1])
    out = outs[0]
    # Last column is 179.75: a source point, so it copies it.
    assert node(out, 10, 179.75) == np.rint(f(10, 179.75))
    # The node before it sits between 179.5 and 179.75.
    assert node(out, 10, 179.625) == np.rint((f(10, 179.5) + f(10, 179.75)) / 2)
    # First column is -180 == 180: the source's wrap point.
    assert node(out, 10, -180) == np.rint(f(10, 180))
    assert node(out, 10, -179.875) == np.rint((f(10, -180) + f(10, -179.75)) / 2)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_nan_and_clip(layout):
    def holed(lat, lon):
        v = f(lat, lon).astype(float)
        v[(lat == 10.25) & (lon % 360 == 20)] = np.nan          # one point
        blk = np.isin(lat, [30, 30.25]) & np.isin(lon % 360, [40, 40.25])
        v[blk] = np.nan                                        # 2x2 block
        v[(lat == 50) & (lon % 360 == 60)] = 140                # over range
        return v
    out = to_feed(*source(layout, holed))
    # Midpoints beside the NaN point average the one neighbour that is left.
    assert node(out, 10.125, 20) == np.rint(f(10, 20))
    assert node(out, 10.25, 20) == 255
    four = [f(10, 20), f(10, 20.25), f(10.25, 20.25)]            # (10.25, 20) is NaN
    assert node(out, 10.125, 20.125) == np.rint(np.mean(four))
    assert node(out, 30.125, 40.125) == 255
    assert node(out, 50, 60) == 100
