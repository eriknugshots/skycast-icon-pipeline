from datetime import datetime, timezone

import numpy as np
import pytest

import hrrr.fogtiles as ft
from hrrr.fog import FOG_FIELDS
from pipeline.columns import decode_columns

RUN = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
# A small fake Lambert grid: first point 43.0 N, 122.5 W, 3 km cells. It covers
# N44W122 whole and only the east half of N44W123.
GRID = {"Latin1InDegrees": 38.5, "Latin2InDegrees": 38.5, "LoVInDegrees": 239.0,
        "latitudeOfFirstGridPointInDegrees": 43.0, "longitudeOfFirstGridPointInDegrees": 237.5,
        "DxInMetres": 3000.0, "DyInMetres": 3000.0, "Nx": 100, "Ny": 80}


def fake_read(run, fh, get):
    nx, ny = GRID["Nx"], GRID["Ny"]
    j, i = np.divmod(np.arange(nx * ny), nx)
    out = {"lat": np.zeros(nx * ny), "lon": np.zeros(nx * ny), "Nx": nx, "Ny": ny, "grid": dict(GRID)}
    for n, key in enumerate(FOG_FIELDS):
        base = {"TMP": 10.0, "RH": 60.0, "HGT": 500.0 + 10 * n}[key[0]]
        out[key] = base + 0.01 * i + 0.02 * j + fh + 0.1 * n
    return out


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(ft, "read_fog_fields", fake_read)


def test_constants():
    assert (ft.FOG_SPACING_MDEG, ft.FOG_TILE_DEG, ft.FOG_N) == (25, 1, 41)
    assert ft.FOG_BLOCK_HOURS == 6


def test_tile_name():
    assert ft.tile_name(44, -122) == "N44W122"
    assert ft.tile_name(21, -60) == "N21W060"


def test_tile_nodes():
    lat, lon = ft.tile_nodes(44, -122)
    assert lat.shape == lon.shape == (41, 41)
    assert abs(lat[0, 0] - 44.0) < 1e-9 and abs(lon[0, 0] + 122.0) < 1e-9
    assert abs(lat[40, 40] - 45.0) < 1e-9 and abs(lon[40, 40] + 121.0) < 1e-9
    assert lat[1, 0] > lat[0, 0] and lon[0, 1] > lon[0, 0]


def test_build_fog_writes_blocks(patched, tmp_path):
    entry = ft.build_fog(RUN, tmp_path, None, 8)
    root = tmp_path / "fog" / "2026111012"
    assert (root / "N44W122" / "0.icc").is_file() and (root / "N44W122" / "1.icc").is_file()
    assert not (root / "N44W122" / "2.icc").exists()
    assert not (root / "N44W123").exists()
    assert "N44W122" in entry["tileNames"] and "N44W123" not in entry["tileNames"]
    d = decode_columns((root / "N44W122" / "0.icc").read_bytes())
    base = int(RUN.timestamp() // 3600)
    assert d["step_hours"] == [base + k for k in range(6)]
    assert (d["sw_lat"], d["sw_lon"], d["spacing_mdeg"], d["cols"], d["rows"]) == (44, -122, 25, 41, 41)
    d1 = decode_columns((root / "N44W122" / "1.icc").read_bytes())
    assert d1["step_hours"] == [base + 6, base + 7]
    assert len(d["spec"]) == 27


def test_build_fog_values(patched, tmp_path):
    from hrrr.fog import lcc_params, lcc_ij
    from pipeline.columns import dequantize
    ft.build_fog(RUN, tmp_path, None, 2)
    d = decode_columns((tmp_path / "fog/2026111012/N44W122/0.icc").read_bytes())
    t = dequantize(d["fields"][0], d["spec"][0][3], d["spec"][0][4])   # [row][col][step]
    lat, lon = ft.tile_nodes(44, -122)
    fi, fj = lcc_ij(lat, lon, lcc_params(GRID))
    # TMP 1000 mb is field 0: 10 + 0.01 i + 0.02 j + fh. Rows are latitude (south
    # first), columns longitude (west first); a swapped axis moves these nodes.
    for r, c in ((0, 40), (40, 0), (0, 0), (40, 40), (13, 27)):
        for step in (0, 1):
            want = 10 + 0.01 * fi[r, c] + 0.02 * fj[r, c] + step
            assert abs(t[r, c, step] - want) < 0.06, (r, c, step)
    assert abs(fi[0, 40] - fi[40, 0]) > 20   # the two corners really differ


def test_build_fog_entry(patched, tmp_path):
    entry = ft.build_fog(RUN, tmp_path, None, 8)
    assert entry["tiles"] == "fog/2026111012"
    assert entry["format"] == "ICC1" and entry["run"] == "2026-11-10T12Z"
    assert entry["runMs"] == int(RUN.timestamp() * 1000)
    assert entry["blockHours"] == ft.FOG_BLOCK_HOURS
    assert entry["tileDeg"] == 1 and entry["spacingMdeg"] == 25
    assert len(entry["stepsMs"]) == 8
    assert entry["stepsMs"][1] - entry["stepsMs"][0] == 3600_000


@pytest.mark.parametrize("steps", [0, [], [3, 3], [2, 1]])
def test_build_fog_rejects_bad_steps(patched, tmp_path, steps):
    with pytest.raises(ValueError):
        ft.build_fog(RUN, tmp_path, None, steps)
    assert not (tmp_path / "fog").exists()
