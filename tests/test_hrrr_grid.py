import numpy as np
import pytest

from hrrr.grid import SQUARES, bin_index, box_average
from pipeline.squares import SIDE, square_name


def test_squares_count():
    assert len(SQUARES) == len(range(15, 60, 5)) * len(range(-140, -55, 5))


def _grid_with_node(sw_lat, sw_lon, r, c, values, jitter):
    lat0 = sw_lat + r * 0.125
    lon0 = sw_lon + c * 0.125
    lat = np.array([lat0 + j[0] for j in jitter])
    lon = np.array([lon0 + j[1] for j in jitter])
    return lat, lon, np.array(values, dtype=float)


def test_box_average_means_and_gaps():
    lat, lon, vals = _grid_with_node(40, -110, 2, 5, [10, 20, 61], [(0.0, 0.0), (0.03, -0.03), (-0.04, 0.02)])
    bins = bin_index(lat, lon)
    out = box_average(vals, bins)
    name = square_name(40, -110)
    assert name == "N40W110"
    grid = out[name]
    assert grid.shape == (SIDE, SIDE) and grid.dtype == np.uint8
    assert grid[2][5] == round((10 + 20 + 61) / 3)
    mask = np.ones((SIDE, SIDE), bool)
    mask[2, 5] = False
    assert (grid[mask] == 255).all()
    # squares the points do not touch are all gaps
    assert (out[square_name(15, -140)] == 255).all()


def test_box_average_clips():
    lat, lon, vals = _grid_with_node(40, -110, 0, 0, [140], [(0.0, 0.0)])
    out = box_average(vals, bin_index(lat, lon))
    assert out["N40W110"][0][0] == 100


def test_box_average_skips_nan_and_negative():
    lat, lon, vals = _grid_with_node(40, -110, 3, 3, [np.nan, -1, 50], [(0, 0), (0.01, 0), (0, 0.01)])
    out = box_average(vals, bin_index(lat, lon))
    assert out["N40W110"][3][3] == 50
    lat, lon, vals = _grid_with_node(40, -110, 3, 3, [np.nan, -1], [(0, 0), (0.01, 0)])
    out = box_average(vals, bin_index(lat, lon))
    assert out["N40W110"][3][3] == 255
