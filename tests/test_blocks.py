import struct
import zlib
import numpy as np
import pytest
from pipeline.blocks import BLOCK_STEPS, encode_blocks, read_header, decode_blocks


def _cube(steps, fields=4, seed=1):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 101, size=(steps, fields, 41, 41), dtype=np.uint8)


def test_round_trip_7_steps():
    cube = _cube(7)
    hours = [500_000 + k for k in range(7)]
    buf = encode_blocks(b"ICL2", 40, -125, hours, cube)
    out = decode_blocks(buf, 4)
    assert np.array_equal(out["cube"], cube)
    assert out["magic"] == b"ICL2"
    assert (out["sw_lat"], out["sw_lon"]) == (40, -125)
    assert (out["cols"], out["rows"], out["steps"]) == (41, 41, 7)
    assert out["step_hours"] == hours
    assert out["block_steps"] == BLOCK_STEPS == 6
    assert len(out["blocks"]) == 2
    assert out["data_start"] == 14 + 4 * 7 + 4 + 8 * 2 == 62
    lengths = [ln for _, ln in out["blocks"]]
    assert out["data_start"] + sum(lengths) == len(buf)
    offsets = [off for off, _ in out["blocks"]]
    assert offsets[0] == 0
    assert all(offsets[i + 1] == offsets[i] + lengths[i] for i in range(len(offsets) - 1))


def test_each_block_decodes_alone():
    cube = _cube(7)
    buf = encode_blocks(b"ICL2", 40, -125, list(range(7)), cube)
    head = read_header(buf)
    for i, (off, ln) in enumerate(head["blocks"]):
        n = 6 if i == 0 else 1
        body = zlib.decompress(buf[head["data_start"] + off: head["data_start"] + off + ln])
        got = np.frombuffer(body, dtype=np.uint8).reshape(n, 4, 41, 41)
        assert np.array_equal(got, cube[6 * i: 6 * i + n])


def test_153_steps_header_is_838_bytes():
    cube = np.zeros((153, 4, 41, 41), dtype=np.uint8)
    buf = encode_blocks(b"ICL2", 0, 0, list(range(153)), cube)
    head = read_header(buf)
    assert head["data_start"] == 838
    assert len(head["blocks"]) == 26


def test_two_field_thickness_magic():
    cube = _cube(3, fields=2)
    buf = encode_blocks(b"ICT1", -5, 170, [7, 8, 9], cube)
    assert buf[:4] == b"ICT1"
    assert struct.unpack_from("<hhHHH", buf, 4) == (-5, 170, 41, 41, 3)
    assert np.array_equal(decode_blocks(buf, 2)["cube"], cube)


def test_encode_rejects():
    ok = _cube(2)
    hours = [1, 2]
    with pytest.raises(ValueError):
        encode_blocks(b"ICL2", 0, 0, hours, ok.astype(np.int16))
    with pytest.raises(ValueError):
        encode_blocks(b"ICL2", 0, 0, hours, ok[:, :, :40, :40])
    with pytest.raises(ValueError):
        encode_blocks(b"ICL2", 0, 0, [2, 1], ok)
    with pytest.raises(ValueError):
        encode_blocks(b"ICL2", 0, 0, [1, 1], ok)
    with pytest.raises(ValueError):
        encode_blocks(b"ICL2", 0, 0, [1, 2, 3], ok)


def test_decode_rejects():
    cube = _cube(2)
    buf = encode_blocks(b"ICL2", 0, 0, [1, 2], cube)
    with pytest.raises(ValueError):
        decode_blocks(b"ICL1" + buf[4:], 4)
    # 4 fields of data cannot be read as 3 fields: the block length does not fit.
    with pytest.raises(ValueError):
        decode_blocks(buf, 3)
