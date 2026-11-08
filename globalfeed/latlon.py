# globalfeed/latlon.py — put a regular 0.25 degree field onto the feed's 0.125 degree nodes.
#
# GFS and AIFS both publish 721 x 1440 regular grids (GFS lon 0..359.75, AIFS
# lon -180..179.75, both lat 90 -> -90). The feed's world grid is twice as fine
# in each direction and shares the source's points, so every other node sits on
# a source point and the rest are the bilinear mean of their 2 or 4 neighbours.
# NaN neighbours are left out of that mean; a node with none left is 255, the
# feed's "no data".
import numpy as np

from pipeline.squares import NX, NY

STEP = 0.25
SRC_NY, SRC_NX = 721, 1440
NODATA = 255


def _sorted_grid(lat, lon, values):
    """Flat eccodes arrays -> [721][1440], lat from -90, lon from -180."""
    rows = np.rint((np.asarray(lat, dtype=float) + 90) / STEP).astype(int)
    cols = np.rint(((np.asarray(lon, dtype=float) + 180) % 360) / STEP).astype(int) % SRC_NX
    grid = np.full((SRC_NY, SRC_NX), np.nan)
    grid[rows, cols] = np.asarray(values, dtype=float)
    return grid


def to_feed(lat, lon, values):
    """-> uint8[NY][NX] on the feed's layout: node (r, c) at (-90 + r*DEG, -180 + c*DEG)."""
    grid = _sorted_grid(lat, lon, values)
    ok = ~np.isnan(grid)
    s = np.where(ok, grid, 0.0)
    n = ok.astype(float)

    # Rows: even fine rows are source rows, odd ones sit between two.
    rs = np.empty((NY, SRC_NX))
    rn = np.empty((NY, SRC_NX))
    rs[0::2], rn[0::2] = s, n
    rs[1::2], rn[1::2] = s[:-1] + s[1:], n[:-1] + n[1:]

    # Columns: the fine grid stops at 179.75, so the last odd column (between
    # 179.75 and the wrap) does not exist; every other odd one has a pair, and
    # the roll keeps that pairing honest if the source is ever widened.
    fs = np.empty((NY, NX))
    fn = np.empty((NY, NX))
    fs[:, 0::2], fn[:, 0::2] = rs, rn
    fs[:, 1::2] = (rs + np.roll(rs, -1, axis=1))[:, :NX // 2]
    fn[:, 1::2] = (rn + np.roll(rn, -1, axis=1))[:, :NX // 2]

    out = np.full((NY, NX), NODATA, dtype=np.uint8)
    have = fn > 0
    out[have] = np.rint(np.clip(fs[have] / fn[have], 0, 100)).astype(np.uint8)
    return out
