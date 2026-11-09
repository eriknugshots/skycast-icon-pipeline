import numpy as np
import pytest

from pipeline.squares import SIDE, square_name
from rrfs.grid import NODE_COLS, NODE_ROWS, SQUARES, bin_index, node_average, cut_squares


def at(lat, lon):
    return np.array([lat]), np.array([lon])


def src(points):
    """points: [(row, col, value)] on the node grid (lat = 0.125 row, lon = -180 + 0.125 col)."""
    lat = np.array([r * 0.125 for r, c, v in points])
    lon = np.array([-180 + c * 0.125 for r, c, v in points])
    return lat, lon, np.array([v for r, c, v in points], dtype=float)


def test_squares_cover_lat_0_80_lon_m180_m50():
    assert SQUARES[0] == (0, -180) and SQUARES[-1] == (75, -55)
    assert len(SQUARES) == 16 * 26 == 416
    assert (NODE_ROWS, NODE_COLS) == (641, 1041)


def test_empty_node_takes_mean_of_voting_neighbours():
    # voters: (10,10)=40 and (10,12)=60; the node between them, (10,11), has neighbours
    # (10,10) and (10,12) only -> mean (40 + 60) / 2 = 50. A node two steps from any voter stays 255.
    lat, lon, v = src([(10, 10, 40), (10, 12, 60)])
    g = node_average(v, bin_index(lat, lon))
    assert g[10, 10] == 40 and g[10, 12] == 60
    assert g[10, 11] == 50
    # (11,11) touches (10,10) and (10,12) diagonally -> also 50; (12,11) is two rows from both -> 255
    assert g[11, 11] == 50
    assert g[12, 11] == 255
    assert g[30, 30] == 255


def test_fill_uses_voters_only_not_other_filled_nodes():
    lat, lon, v = src([(10, 10, 30)])
    g = node_average(v, bin_index(lat, lon))
    assert g[10, 11] == 30 and g[11, 11] == 30      # all eight neighbours of the voter
    assert g[10, 12] == 255                           # two away: a filled node is not a voter


def test_nan_cells_do_not_vote():
    lat, lon, v = src([(10, 10, np.nan), (10, 12, 70)])
    g = node_average(v, bin_index(lat, lon))
    assert g[10, 10] == 255        # the NaN cell cast no vote and (10,12) is two nodes away
    assert g[10, 11] == 70         # filled from the one voter next to it


def test_several_voters_average_and_clip():
    lat, lon, v = src([(20, 20, 10), (20, 20, 20), (20, 20, 61), (22, 22, 140)])
    g = node_average(v, bin_index(lat, lon))
    assert g[20, 20] == round((10 + 20 + 61) / 3)      # 30
    assert g[22, 22] == 100


def test_fill_crosses_a_square_edge():
    # row 40 is lat 5.0: the north edge of square N00 and the south edge of square N05 (row 0)
    lat, lon, v = src([(39, 50, 20), (41, 50, 80)])
    g = node_average(v, bin_index(lat, lon))
    assert g[40, 50] == 50
    cut = cut_squares(g[None])
    # column 50 of the grid is column 10 of square W175 (grid columns 40..80)
    s_lo = cut[square_name(0, -175)]
    s_hi = cut[square_name(5, -175)]
    assert s_lo.shape == (1, SIDE, SIDE)
    assert s_lo[0, 40, 10] == 50 and s_hi[0, 0, 10] == 50


def test_points_outside_the_domain_are_ignored():
    lat, lon, v = at(85.0, -100.0) + (np.array([50.0]),)
    g = node_average(v, bin_index(lat, lon))
    assert (g == 255).all()
    lat, lon, v = at(40.0, 20.0) + (np.array([50.0]),)
    assert (node_average(v, bin_index(lat, lon)) == 255).all()


def test_cut_squares_names_and_all_255_far_squares():
    lat, lon, v = src([(10, 10, 40)])
    g = node_average(v, bin_index(lat, lon))
    cut = cut_squares(np.stack([g, g]))
    assert set(cut) == {square_name(a, b) for a, b in SQUARES}
    assert cut["N00W180"].shape == (2, SIDE, SIDE)
    assert cut["N00W180"][0, 10, 10] == 40
    assert (cut[square_name(75, -55)] == 255).all()
