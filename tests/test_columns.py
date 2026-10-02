import struct
import zlib
import numpy as np
import pytest
from pipeline import columns
from pipeline.columns import (encode_columns, decode_columns, field_spec, quantize, dequantize, header_bytes,
                              lattice, slice_nodes, square_nodes, side, spacing_deg, MISSING, STATIC)
from pipeline.squares import row_indices, col_indices, slice_square, NX, NY, SIDE, DEG

PLEVELS = [1000, 950, 925, 900, 850, 800, 700]


def _codes(steps, stride, seed=1, high=40000):
    spec = field_spec(PLEVELS)
    n = side(stride)
    ndyn = sum(1 for f in spec if f[1] != STATIC)
    nsta = len(spec) - ndyn
    rng = np.random.default_rng(seed)
    return (spec, rng.integers(0, high, size=(steps, ndyn, n, n), dtype=np.uint16),
            rng.integers(0, high, size=(nsta, n, n), dtype=np.uint16))


def _body_by_the_spec(blob):
    """An independent reading of the byte layout in docs/column-feed-design.md:
    header, hours, field table, then zlib(low plane ‖ high plane) of deltas
    whose running sum mod 65536 is the code stream."""
    magic, sw_lat, sw_lon, sp, cols, rows, steps, nf = struct.unpack_from("<4shhHHHHH", blob, 0)
    off = 18 + 4 * steps
    table = [struct.unpack_from("<BBHff", blob, off + 12 * i) for i in range(nf)]
    raw = zlib.decompress(blob[off + 12 * nf:])
    n = len(raw) // 2
    codes, acc = [], 0
    for i in range(n):
        acc = (acc + (raw[i] | raw[n + i] << 8)) % 65536
        codes.append(acc)
    return (sw_lat, sw_lon, sp, cols, rows, steps), table, codes


def test_round_trip():
    spec, dyn, sta = _codes(steps=4, stride=4)
    hours = [496_000, 496_001, 496_002, 496_005]
    blob = encode_columns(40, -125, 4, hours, spec, dyn, sta)
    d = decode_columns(blob)
    assert (d["sw_lat"], d["sw_lon"], d["step_hours"]) == (40, -125, hours)
    assert d["spec"] == [(k, lt, lv, np.float32(sc), np.float32(of)) for k, lt, lv, sc, of in spec]
    di = si = 0
    for f, got in zip(spec, d["fields"]):
        if f[1] == STATIC:
            assert np.array_equal(got, sta[si]); si += 1
        else:
            assert np.array_equal(got, np.moveaxis(dyn[:, di], 0, -1)); di += 1


def test_byte_layout_is_the_documented_one():
    stride = 4
    spec, dyn, sta = _codes(steps=2, stride=stride)
    blob = encode_columns(-5, 170, stride, [7, 9], spec, dyn, sta)
    head, table, codes = _body_by_the_spec(blob)
    n = (SIDE - 1) // stride + 1
    assert blob[:4] == b"ICC1"
    assert head == (-5, 170, round(stride * DEG * 1000), n, n, 2)
    assert struct.unpack_from("<2I", blob, 18) == (7, 9)
    assert [(k, lt, lv) for k, lt, lv, _, _ in table] == [(k, lt, lv) for k, lt, lv, _, _ in spec]
    # Body order: field by field; a dynamic field node by node (south row
    # first, west column first), each node's steps together; static: one each.
    expect, di, si = [], 0, 0
    for f in spec:
        if f[1] == STATIC:
            expect += [int(sta[si, r, c]) for r in range(n) for c in range(n)]; si += 1
        else:
            expect += [int(dyn[s, di, r, c]) for r in range(n) for c in range(n) for s in range(2)]; di += 1
    assert codes == expect


def test_field_table_names_the_app_fields_and_units():
    spec = field_spec(PLEVELS)
    T, RH, GH, HS = columns.T, columns.RH, columns.GH, columns.HSURF
    P, H = columns.PRESSURE_HPA, columns.HEIGHT_AGL_M
    assert [(k, lt, lv) for k, lt, lv, _, _ in spec] == (
        [(k, P, p) for p in PLEVELS for k in (T, RH, GH)] + [(T, H, 2), (RH, H, 2), (T, H, 80), (HS, STATIC, 0)])
    assert len(spec) == 3 * len(PLEVELS) + 4


def test_missing_is_the_sentinel_both_ways():
    scale, offset = 0.1, -150.0
    vals = np.array([np.nan, -20.0, np.inf, 15.04])
    q = quantize(vals, scale, offset)
    assert q[0] == MISSING and q[2] == MISSING and MISSING == 65535
    back = dequantize(q, scale, offset)
    assert np.isnan(back[0]) and np.isnan(back[2])
    spec, dyn, sta = _codes(steps=3, stride=8)
    dyn[1, 4, 2, 3] = MISSING
    sta[0, 0, 0] = MISSING
    d = decode_columns(encode_columns(0, 0, 8, [1, 2, 3], spec, dyn, sta))
    assert d["fields"][4][2, 3, 1] == MISSING
    assert d["fields"][-1][0, 0] == MISSING


def test_quantize_is_within_half_a_step_and_clamps():
    rng = np.random.default_rng(3)
    for scale, offset in [(0.1, -150.0), (1.0, 0.0), (1.0, -1000.0)]:
        sc, of = float(np.float32(scale)), float(np.float32(offset))
        vals = of + sc * rng.uniform(0, 60000, 1000)
        back = dequantize(quantize(vals, scale, offset), scale, offset)
        assert np.all(np.abs(back - vals) <= sc / 2 + 1e-9 * np.abs(vals).max())
    q = quantize(np.array([-1e9, 1e9]), 1.0, 0.0)
    assert q.tolist() == [0, MISSING - 1]


def _compress_bound(n):
    # zlib's compressBound(): n + (n >> 12) + (n >> 14) + (n >> 25) + 13.
    return n + (n >> 12) + (n >> 14) + (n >> 25) + 13


@pytest.mark.parametrize("stride", [2, 4, 8])
def test_size_never_exceeds_the_bound_the_format_implies(stride):
    steps = 5
    spec, dyn, sta = _codes(steps=steps, stride=stride, high=65535)      # incompressible
    n = side(stride)
    values = n * n * (sum(steps for f in spec if f[1] != STATIC) + sum(1 for f in spec if f[1] == STATIC))
    blob = encode_columns(0, 0, stride, list(range(steps)), spec, dyn, sta)
    assert len(blob) <= header_bytes(steps, len(spec)) + _compress_bound(2 * values)
    assert header_bytes(steps, len(spec)) == 18 + 4 * steps + 12 * len(spec)


def test_smooth_series_compress_far_below_the_raw_size():
    # The point of the deltas: an hourly series moving a few codes per hour.
    stride, steps = 4, 48
    spec, _, sta = _codes(steps=steps, stride=stride)
    n = side(stride)
    ndyn = sum(1 for f in spec if f[1] != STATIC)
    rng = np.random.default_rng(5)
    walk = 30000 + np.cumsum(rng.integers(-3, 4, size=(steps, ndyn, n, n)), axis=0)
    blob = encode_columns(0, 0, stride, list(range(steps)), spec, walk.astype(np.uint16), sta)
    assert len(blob) < 0.5 * 2 * walk.size


def test_refuses_bad_input():
    spec, dyn, sta = _codes(steps=2, stride=4)
    with pytest.raises(ValueError):
        encode_columns(0, 0, 4, [2, 1], spec, dyn, sta)
    with pytest.raises(ValueError):
        encode_columns(0, 0, 4, [1], spec, dyn, sta)
    with pytest.raises(ValueError):
        encode_columns(0, 0, 4, [1, 2], spec, dyn.astype(np.int32), sta)
    with pytest.raises(ValueError):
        encode_columns(0, 0, 8, [1, 2], spec, dyn, sta)                  # 11-node arrays, 6-node stride
    with pytest.raises(ValueError):
        side(3)
    with pytest.raises(ValueError):
        decode_columns(b"ICL1" + bytes(40))


@pytest.mark.parametrize("stride", [2, 4, 8])
def test_square_nodes_are_every_stride_th_cloud_sample(stride):
    world = np.arange(NY * NX, dtype=np.int64).reshape(1, NY, NX)
    lat_arr = lattice(world, stride)
    for sw_lat, sw_lon in [(40, -125), (-90, -180), (85, 175), (0, 0)]:
        want = slice_square(world, sw_lat, sw_lon)[:, ::stride, ::stride]
        assert np.array_equal(slice_nodes(lat_arr, sw_lat, sw_lon, stride), want)
    rows, cols = square_nodes(85, 175, stride)
    assert cols[-1] == 0                                   # 180° E wraps to the grid's column 0
    assert len(rows) == len(cols) == side(stride) == (SIDE - 1) // stride + 1
    assert spacing_deg(stride) == stride * DEG
