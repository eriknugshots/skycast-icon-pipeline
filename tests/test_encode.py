import struct
import zlib
import numpy as np
import pytest
from pipeline.encode import encode_square, decode_square, MISSING, FIELD_COUNT


def _cube(steps=3, n=41, seed=1):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 101, size=(steps, FIELD_COUNT, n, n), dtype=np.uint8)


def test_round_trip():
    cube = _cube()
    hours = [496_000, 496_001, 496_002]
    blob = encode_square(sw_lat=40, sw_lon=-125, step_hours=hours, cube=cube)
    out = decode_square(blob)
    assert out["sw_lat"] == 40 and out["sw_lon"] == -125
    assert out["step_hours"] == hours
    assert out["cube"].shape == (3, FIELD_COUNT, 41, 41)
    assert np.array_equal(out["cube"], cube)


def test_header_layout_is_the_documented_one():
    cube = _cube(steps=1)
    blob = encode_square(sw_lat=-5, sw_lon=170, step_hours=[7], cube=cube)
    assert blob[:4] == b"ICL1"
    sw_lat, sw_lon, cols, rows, steps = struct.unpack_from("<hhHHH", blob, 4)
    assert (sw_lat, sw_lon, cols, rows, steps) == (-5, 170, 41, 41, 1)
    assert struct.unpack_from("<I", blob, 14)[0] == 7
    body = zlib.decompress(blob[18:])
    assert len(body) == FIELD_COUNT * 41 * 41


def test_missing_survives_and_values_are_percent():
    cube = _cube(steps=2)
    cube[1, 2, 0, 0] = MISSING
    out = decode_square(encode_square(sw_lat=0, sw_lon=0, step_hours=[1, 2], cube=cube))
    assert out["cube"][1, 2, 0, 0] == MISSING
    assert MISSING == 255


def test_refuses_unsorted_steps_and_bad_shapes():
    cube = _cube(steps=2)
    with pytest.raises(ValueError):
        encode_square(sw_lat=0, sw_lon=0, step_hours=[2, 1], cube=cube)
    with pytest.raises(ValueError):
        encode_square(sw_lat=0, sw_lon=0, step_hours=[1], cube=cube)          # 2 steps, 1 hour
    with pytest.raises(ValueError):
        encode_square(sw_lat=0, sw_lon=0, step_hours=[1, 2], cube=cube.astype(np.int16))
