import numpy as np
import pytest
from pipeline.squares import (NX, NY, DEG, SQUARE_DEG, square_name, parse_name, all_squares,
                              col_indices, row_indices, slice_square)


def test_world_grid_dimensions_match_dwd_kit():
    assert (NX, NY, DEG, SQUARE_DEG) == (2879, 1441, 0.125, 5)


def test_names_are_sw_corners():
    assert square_name(40, -125) == "N40W125"
    assert square_name(-5, 170) == "S05E170"
    assert square_name(0, 0) == "N00E000"
    assert parse_name("S05E170") == (-5, 170)
    assert parse_name("N40W125") == (40, -125)


def test_all_squares_cover_the_globe_once():
    names = all_squares()
    assert len(names) == 36 * 72
    assert names[0] == "S90W180" and names[-1] == "N85E175"
    assert len(set(names)) == len(names)


def test_row_and_col_indices_are_41_long_and_edge_aligned():
    assert row_indices(-90) == list(range(0, 41))
    assert row_indices(85) == list(range(1400, 1441))
    assert col_indices(-180) == list(range(0, 41))
    assert col_indices(-125) == list(range(440, 481))


def test_the_east_edge_wraps_to_the_dateline_column():
    cols = col_indices(175)
    assert cols[:2] == [2840, 2841]
    assert cols[-1] == 0            # 180° E is the -180° column


def test_slice_square_takes_the_41x41_block_per_field():
    world = np.arange(4 * NY * NX, dtype=np.int32).reshape(4, NY, NX).astype(np.uint8)
    block = slice_square(world, 40, -125)
    assert block.shape == (4, 41, 41)
    assert block[0, 0, 0] == world[0, 1040, 440]
    assert block[3, 40, 40] == world[3, 1080, 480]


def test_slice_square_at_the_dateline_uses_the_wrapped_column():
    world = np.zeros((4, NY, NX), dtype=np.uint8)
    world[:, :, 0] = 7
    block = slice_square(world, 0, 175)
    assert (block[:, :, 40] == 7).all()


def test_bad_names_raise():
    with pytest.raises(ValueError):
        parse_name("N4W125")
    with pytest.raises(ValueError):
        square_name(41, -125)
