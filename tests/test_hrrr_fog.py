from datetime import datetime, timezone

import numpy as np
import eccodes
import pytest

import hrrr.fog

from hrrr.fog import (fog_ranges, grid_keys, FOG_FIELDS, prs_url, lcc_params, lcc_ij, sample_bilinear,
                      read_fog_fields)


def test_fog_fields_order():
    assert FOG_FIELDS[0] == ("TMP", "1000 mb")
    assert FOG_FIELDS[2] == ("HGT", "1000 mb")
    assert FOG_FIELDS[-3:] == (("TMP", "2 m above ground"), ("RH", "2 m above ground"), ("HGT", "surface"))
    assert len(FOG_FIELDS) == 27


def test_prs_url():
    u = prs_url(datetime(2026, 11, 10, 12, tzinfo=timezone.utc), 6)
    assert u.endswith("hrrr.20261110/conus/hrrr.t12z.wrfprsf06.grib2")


# Printed by tools/hrrr_fog_measure.py's real read of 2026-11-11 18Z f00 (wrfprsf),
# docs/hrrr-fog-measure.md "Grid keys for Task 2". Longitudes are the GRIB's 0..360;
# lcc_ij accepts either convention.
KEYS = {
    "Latin1InDegrees": 38.5, "Latin2InDegrees": 38.5, "LoVInDegrees": 262.5,
    "latitudeOfFirstGridPointInDegrees": 21.138123,
    "longitudeOfFirstGridPointInDegrees": 237.280472,
    "DxInMetres": 3000.0, "DyInMetres": 3000.0, "Nx": 1799, "Ny": 1059,
}
POINTS = [  # (i, j, lat, lon 0..360)
    (0, 0, 21.138123, 237.280472),
    (900, 530, 38.524222, 262.528505),
    (1798, 1058, 47.842195, 299.082807),
]


def test_lcc_roundtrip():
    p = lcc_params(KEYS)
    for i, j, lat, lon in POINTS:
        for lo in (lon, lon - 360):
            fi, fj = lcc_ij(lat, lo, p)
            assert abs(fi - i) < 0.01 and abs(fj - j) < 0.01, (i, j, fi, fj)


def test_sample_bilinear():
    v = np.arange(12.0).reshape(3, 4)
    assert float(sample_bilinear(v, 1.5, 0.5)) == 3.5
    assert np.isnan(sample_bilinear(v, -0.1, 0.0))
    w = v.copy()
    w[1, 2] = np.nan
    assert np.isnan(sample_bilinear(w, 1.5, 0.5))
    assert float(sample_bilinear(v, 3.0, 2.0)) == 11.0  # last node is inside


def _msg(values, missing=False):
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, x in (("Ni", 2), ("Nj", 2), ("latitudeOfFirstGridPointInDegrees", 41.0),
                 ("latitudeOfLastGridPointInDegrees", 40.0),
                 ("longitudeOfFirstGridPointInDegrees", 250.0),
                 ("longitudeOfLastGridPointInDegrees", 251.0),
                 ("iDirectionIncrementInDegrees", 1.0), ("jDirectionIncrementInDegrees", 1.0)):
        eccodes.codes_set(h, k, x)
    if missing:
        eccodes.codes_set(h, "bitmapPresent", 1)
    eccodes.codes_set(h, "bitsPerValue", 24)
    eccodes.codes_set_values(h, np.array(values, dtype=float))
    b = eccodes.codes_get_message(h)
    eccodes.codes_release(h)
    return b


def test_read_fog_fields_units(monkeypatch):
    monkeypatch.setattr(hrrr.fog, "grid_keys", lambda h: dict(KEYS, Nx=2, Ny=2))
    run = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    url = prs_url(run, 3)
    miss = 9999.0
    msgs, lines, pos = {}, [], 0
    for n, key in enumerate(FOG_FIELDS, 1):
        if key == ("TMP", "1000 mb"):
            m = _msg([283.15, 273.15, 263.15, 253.15])
        elif key == ("RH", "1000 mb"):
            m = _msg([miss, 50, 60, 70], missing=True)  # first cell missing
        else:
            m = _msg([1, 2, 3, 4])
        msgs[key] = (pos, pos + len(m) - 1, m)
        lines.append(f"{n}:{pos}:d=2026111012:{key[0]}:{key[1]}:3 hour fcst:")
        pos += len(m)
    idx = "\n".join(lines) + "\n"

    def get(u, rng=None):
        if u == url + ".idx":
            return idx.encode()
        for s, e, m in msgs.values():
            if rng == (s, e if e < pos - 1 else None) or rng == (s, e):
                return m
        raise AssertionError(rng)

    out = read_fog_fields(run, 3, get)
    assert np.allclose(out[("TMP", "1000 mb")], [10.0, 0.0, -10.0, -20.0], atol=1e-3)
    rh = out[("RH", "1000 mb")]
    assert np.isnan(rh[0]) and np.allclose(rh[1:], [50.0, 60.0, 70.0], atol=1e-3)
    assert np.allclose(out[("HGT", "surface")], [1, 2, 3, 4], atol=1e-3)
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.0, -109.0]
    assert len(out) == 32
    assert out["Nx"] == 2 and out["Ny"] == 2 and out["grid"]["LoVInDegrees"] == 262.5


def test_fog_ranges_bounds_and_missing():
    lines = [f"{n}:{n * 100}:d=2026111012:{k[0]}:{k[1]}:anl:" for n, k in enumerate(FOG_FIELDS, 1)]
    r = fog_ranges("\n".join(lines) + "\n", 0)
    assert r[FOG_FIELDS[0]] == (100, 199)
    assert r[FOG_FIELDS[-1]] == (2700, None)
    with pytest.raises(KeyError):
        fog_ranges("\n".join(lines[:-1]) + "\n", 0)
    with pytest.raises(KeyError):
        fog_ranges("\n".join(lines) + "\n", 3)  # wrong hour label


def test_grid_keys_reads_lcc_message(monkeypatch):
    monkeypatch.setattr(eccodes, "codes_get", lambda h, key: KEYS[key])
    k = grid_keys(object())
    assert k == KEYS
    assert lcc_params(k)["nx"] == 1799


def test_flat_order_is_row_major_south_west_first(monkeypatch):
    """Header contract: n = j*Nx + i, i fastest, first point SW (reshape(Ny, Nx))."""
    monkeypatch.setattr(hrrr.fog, "grid_keys", lambda h: dict(KEYS, Nx=3, Ny=2))
    run = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    url = prs_url(run, 0)

    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, x in (("Ni", 3), ("Nj", 2), ("jScansPositively", 1),
                 ("latitudeOfFirstGridPointInDegrees", 40.0),
                 ("latitudeOfLastGridPointInDegrees", 41.0),
                 ("longitudeOfFirstGridPointInDegrees", 250.0),
                 ("longitudeOfLastGridPointInDegrees", 252.0),
                 ("iDirectionIncrementInDegrees", 1.0), ("jDirectionIncrementInDegrees", 1.0)):
        eccodes.codes_set(h, k, x)
    eccodes.codes_set_values(h, np.arange(6.0))
    m = eccodes.codes_get_message(h)
    eccodes.codes_release(h)
    lines = [f"{n}:{n * 10}:d=2026111012:{k[0]}:{k[1]}:anl:" for n, k in enumerate(FOG_FIELDS, 1)]
    ranges = fog_ranges("\n".join(lines) + "\n", 0)

    def get(u, rng=None):
        return ("\n".join(lines) + "\n").encode() if u.endswith(".idx") else m

    out = read_fog_fields(run, 0, get)
    lat, lon = out["lat"].reshape(2, 3), out["lon"].reshape(2, 3)
    assert (lat[0, 0], lon[0, 0]) == (40.0, -110.0)  # south-west corner
    assert (lat[0, 1], lon[0, 1]) == (40.0, -109.0)  # one step east
    assert lat[1, 0] == 41.0                          # one step north
