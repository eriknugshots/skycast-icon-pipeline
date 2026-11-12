import struct
import numpy as np
import pytest
from pipeline import columns
from pipeline.columns import (encode_columns, encode_upper, decode_columns, field_spec, upper_field_spec,
                              quantize, dequantize, side, STATIC, MAGIC_UPPER)
from pipeline.cluefile import encode_clue_square, decode_clue_square
from pipeline.clues import N_BYTES
from pipeline.dwd import COLUMN_PLEVELS, UPPER_PLEVELS

# Captured by running gh/main's own pipeline code (2f8612c, the live feed with
# WW) before the ICU1 work was rebased onto it — not this branch's code.
FIELD_SPEC_AT_HEAD = [(1, 1, 1000, 0.1, -150.0), (2, 1, 1000, 1.0, 0.0), (3, 1, 1000, 1.0, -1000.0), (1, 1, 950, 0.1, -150.0), (2, 1, 950, 1.0, 0.0), (3, 1, 950, 1.0, -1000.0), (1, 1, 925, 0.1, -150.0), (2, 1, 925, 1.0, 0.0), (3, 1, 925, 1.0, -1000.0), (1, 1, 900, 0.1, -150.0), (2, 1, 900, 1.0, 0.0), (3, 1, 900, 1.0, -1000.0), (1, 1, 850, 0.1, -150.0), (2, 1, 850, 1.0, 0.0), (3, 1, 850, 1.0, -1000.0), (1, 1, 800, 0.1, -150.0), (2, 1, 800, 1.0, 0.0), (3, 1, 800, 1.0, -1000.0), (1, 1, 700, 0.1, -150.0), (2, 1, 700, 1.0, 0.0), (3, 1, 700, 1.0, -1000.0), (1, 2, 2, 0.1, -150.0), (2, 2, 2, 1.0, 0.0), (1, 2, 80, 0.1, -150.0), (5, 2, 0, 1.0, 0.0), (4, 0, 0, 1.0, -1000.0)]
ICC1_AT_HEAD_HEX = "494343310a001400e803060006000200080064000000650000000101e803cdcccc3d000016c30201e8030000803f000000000301e8030000803f00007ac401020200cdcccc3d000016c3020202000000803f0000000001025000cdcccc3d000016c3050200000000803f00000000040000000000803f00007ac4789c7bf1c4911818737284c091121a1fb97183fb509a59e03d31f02fd30881232534de33e006ffa034006b762147"


def _icc1_input():
    spec = field_spec([1000])
    n = side(8)
    ndyn = sum(1 for f in spec if f[1] != STATIC)
    st = ((np.arange(2 * ndyn * n * n) * 37) % 5000 + 1000).astype(np.uint16).reshape(2, ndyn, n, n)
    sta = ((np.arange(n * n) * 11) % 300 + 500).astype(np.uint16).reshape(1, n, n)
    return spec, st, sta


def test_column_feed_unchanged():
    assert field_spec(COLUMN_PLEVELS) == FIELD_SPEC_AT_HEAD


def test_icc1_bytes_identical_to_head():
    spec, st, sta = _icc1_input()
    assert encode_columns(10, 20, 8, [100, 101], spec, st, sta).hex() == ICC1_AT_HEAD_HEX


def test_icu1_roundtrip():
    spec = upper_field_spec([500, 300], singles=(columns.CAPE_CON, columns.HBAS_CON))
    n = side(20)          # 3 x 3 nodes
    assert n == 3
    steps = 2
    phys = {}
    rng = np.random.default_rng(3)
    codes = np.zeros((steps, len(spec), n, n), np.uint16)
    for i, (k, lt, lv, sc, of) in enumerate(spec):
        lo, hi = {columns.T: (-70, 10), columns.RH: (0, 100), columns.GH: (4000, 10000),
                  columns.U: (-80, 80), columns.V: (-80, 80), columns.CAPE_CON: (0, 3000),
                  columns.HBAS_CON: (-500, 6000)}[k]
        v = np.round(rng.uniform(lo, hi, size=(steps, n, n)), 1)
        v[0, 0, 0] = np.nan                       # a missing node
        phys[i] = v
        codes[:, i] = quantize(v, sc, of)
    blob = encode_upper(10, -120, 20, [500, 501], spec, codes, np.zeros((0, n, n), np.uint16))
    assert blob[:4] == b"ICU1" == MAGIC_UPPER
    with pytest.raises(ValueError):
        decode_columns(blob)                      # the ICC1 reader refuses it
    d = decode_columns(blob, magic=b"ICU1")
    assert (d["sw_lat"], d["sw_lon"], d["spacing_mdeg"], d["cols"], d["rows"]) == (10, -120, round(columns.spacing_deg(20) * 1000), 3, 3)
    assert d["step_hours"] == [500, 501]
    # scale and offset travel as f32
    assert d["spec"] == [(k, lt, lv, float(np.float32(sc)), float(np.float32(of))) for k, lt, lv, sc, of in spec]
    for i, (k, lt, lv, sc, of) in enumerate(spec):
        got = dequantize(d["fields"][i], sc, of)             # [rows][cols][steps]
        want = np.moveaxis(phys[i], 0, -1)
        assert np.array_equal(np.isnan(got), np.isnan(want))
        ok = ~np.isnan(want)
        assert np.all(np.abs(got[ok] - want[ok]) <= sc / 2 + 1e-6), columns.KIND_NAMES[k]


def test_icu1_quantisation_table():
    q = columns._QUANT
    assert q[columns.U] == (0.1, -200.0) and q[columns.V] == (0.1, -200.0)
    assert q[columns.CAPE_CON] == (1.0, 0.0) and q[columns.CAPE_ML] == (1.0, 0.0)
    assert q[columns.HBAS_CON] == (1.0, -1000.0) and q[columns.HTOP_CON] == (1.0, -1000.0)
    assert (columns.U, columns.V, columns.CAPE_CON, columns.CAPE_ML, columns.HBAS_CON, columns.HTOP_CON) == (6, 7, 8, 9, 10, 11)
    # ICON's -500 m "no convection base" survives: code 500, back to -500.
    c = quantize([-500.0], *q[columns.HBAS_CON])
    assert dequantize(c, *q[columns.HBAS_CON])[0] == -500.0
    spec = upper_field_spec(UPPER_PLEVELS)
    assert len(spec) == 5 * len(UPPER_PLEVELS) + 4


def test_icx1_roundtrip():
    steps, rows, cols = 3, 4, 5
    clues = np.random.default_rng(5).integers(0, 256, size=(steps, rows, cols, N_BYTES), dtype=np.uint8)
    hours = [700, 701, 703]
    blob = encode_clue_square(-5, 175, 250, hours, clues)
    assert blob[:4] == b"ICX1"
    assert struct.unpack_from("<hhHHHHH", blob, 4) == (-5, 175, 250, cols, rows, steps, 27)
    d = decode_clue_square(blob)
    assert (d["sw_lat"], d["sw_lon"], d["spacing_mdeg"], d["cols"], d["rows"], d["clue_bytes"]) == (-5, 175, 250, cols, rows, 27)
    assert d["step_hours"] == hours
    assert np.array_equal(d["clues"], clues)


def test_icx1_node_order_and_validation():
    steps, rows, cols = 2, 2, 3
    clues = np.zeros((steps, rows, cols, N_BYTES), np.uint8)
    clues[1, 1, 2, 0] = 9        # step 1, row 1 (north), col 2 (east)
    blob = encode_clue_square(0, 0, 250, [1, 2], clues)
    import zlib
    raw = zlib.decompress(blob[18 + 4 * steps:])
    node = 1 * cols + 2          # nodes run row by row, then each node's steps
    assert raw[(node * steps + 1) * N_BYTES] == 9
    assert raw.count(9) == 1 and len(raw) == rows * cols * steps * N_BYTES
    with pytest.raises(ValueError):
        encode_clue_square(0, 0, 250, [2, 1], clues)
    with pytest.raises(ValueError):
        encode_clue_square(0, 0, 250, [1, 2], clues.astype(np.int16))
    with pytest.raises(ValueError):
        encode_clue_square(0, 0, 250, [1], clues)
