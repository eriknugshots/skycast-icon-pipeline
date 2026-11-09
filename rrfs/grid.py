# rrfs/grid.py — RRFS's 13 km cells onto the feed's 0.125 degree nodes.
#
# As hrrr/grid.py: every source cell votes for its nearest node and a node's
# value is the mean of its voters (rounded, 0..100). RRFS's cells are about as
# wide as a node, so some nodes get no voter; an empty node takes the mean of
# its voting 8-neighbours (voters only, never other filled nodes), and a node
# with no voting neighbour is 255, the feed's "no data". NaN source cells do not
# vote.
#
# The domain is one array of nodes (lat 0-80, lon -180 to -50) rather than 416
# separate squares, so the fill can cross a square's edge: a square's north and
# east edge nodes are the neighbours' south and west edge nodes. Squares are cut
# out of it afterwards.
import numpy as np

from pipeline.squares import DEG, SIDE, square_name

LAT0, LON0 = 0, -180
SQUARES = [(la, lo) for la in range(0, 80, 5) for lo in range(-180, -50, 5)]
NODE_ROWS = int(round((80 - LAT0) / DEG)) + 1      # 641
NODE_COLS = int(round((-50 - LON0) / DEG)) + 1     # 1041


def bin_index(lat, lon):
    """(idx, flat): idx picks the source cells inside the domain, flat is each
    one's node index (row * NODE_COLS + col). Row 0 is south, col 0 west."""
    r = np.rint((np.asarray(lat) - LAT0) / DEG).astype(int)
    c = np.rint((np.asarray(lon) - LON0) / DEG).astype(int)
    ok = (r >= 0) & (r < NODE_ROWS) & (c >= 0) & (c < NODE_COLS)
    return np.flatnonzero(ok), r[ok] * NODE_COLS + c[ok]


def node_average(values, bins):
    """uint8[NODE_ROWS][NODE_COLS]: the cover at every node, 255 where nothing
    reaches. NaN and negative source values are skipped."""
    idx, flat = bins
    v = values[idx]
    good = ~np.isnan(v) & (v >= 0)
    size = NODE_ROWS * NODE_COLS
    s = np.bincount(flat[good], v[good], size)
    n = np.bincount(flat[good], None, size)
    voted = (n > 0).reshape(NODE_ROWS, NODE_COLS)
    node = np.where(n > 0, np.clip(np.rint(s / np.maximum(n, 1)), 0, 100), 0).reshape(NODE_ROWS, NODE_COLS)
    # Mean of the voting 8-neighbours for the nodes nobody voted for.
    pv = np.pad(np.where(voted, node, 0.0), 1)
    pc = np.pad(voted.astype(float), 1)
    nsum = np.zeros_like(node)
    ncnt = np.zeros_like(node)
    for dr in (0, 1, 2):
        for dc in (0, 1, 2):
            if dr == 1 and dc == 1:
                continue
            nsum += pv[dr:dr + NODE_ROWS, dc:dc + NODE_COLS]
            ncnt += pc[dr:dr + NODE_ROWS, dc:dc + NODE_COLS]
    out = np.full((NODE_ROWS, NODE_COLS), 255, dtype=np.uint8)
    out[voted] = node[voted].astype(np.uint8)
    fill = ~voted & (ncnt > 0)
    out[fill] = np.rint(nsum[fill] / ncnt[fill]).astype(np.uint8)
    return out


def coverage(grid):
    """(nodes with a value, nodes still 255) — for the build's log line."""
    have = int((grid != 255).sum())
    return have, grid.size - have


def cut_squares(grids):
    """grids: uint8 [..., NODE_ROWS, NODE_COLS] -> {square name: uint8
    [..., SIDE, SIDE]} for every SQUARES entry (views into grids)."""
    out = {}
    for sl, so in SQUARES:
        r0 = int(round((sl - LAT0) / DEG))
        c0 = int(round((so - LON0) / DEG))
        out[square_name(sl, so)] = grids[..., r0:r0 + SIDE, c0:c0 + SIDE]
    return out
