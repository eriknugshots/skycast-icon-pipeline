# hrrr/grid.py — box-average HRRR's 3 km cells onto the feed's 0.125 degree nodes.
#
# Every source cell votes for its nearest feed node; a node's value is the mean
# of its voters. SQUARES reaches past HRRR's domain on purpose: the app throws
# Unavailable unless every square a window needs is in the manifest, so squares
# beyond the domain are published all-255 and the alert refuses windows that
# touch 255.
import numpy as np

from pipeline.squares import DEG, SIDE, square_name

SQUARES = [(la, lo) for la in range(15, 60, 5) for lo in range(-140, -55, 5)]


def bin_index(lat, lon):
    """{square name: (mask, flat)}: mask picks the source cells inside the
    square, flat is each one's node index (row * SIDE + col). Row 0 is south,
    col 0 west."""
    out = {}
    for sl, so in SQUARES:
        r = np.rint((lat - sl) / DEG).astype(int)
        c = np.rint((lon - so) / DEG).astype(int)
        ok = (r >= 0) & (r < SIDE) & (c >= 0) & (c < SIDE)
        out[square_name(sl, so)] = (ok, r[ok] * SIDE + c[ok])
    return out


def box_average(values, bins):
    """{square name: uint8[SIDE][SIDE]}: mean of the voters, rounded, clipped
    to 0..100; 255 where no usable source cell falls. NaN and negative source
    values are skipped."""
    out = {}
    for name, (ok, flat) in bins.items():
        v = values[ok]
        good = ~np.isnan(v) & (v >= 0)
        s = np.bincount(flat[good], v[good], SIDE * SIDE)
        n = np.bincount(flat[good], None, SIDE * SIDE)
        m = np.where(n > 0, np.clip(np.rint(s / np.maximum(n, 1)), 0, 100), 255).astype(np.uint8)
        out[name] = m.reshape(SIDE, SIDE)
    return out
