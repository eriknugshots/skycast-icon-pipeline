import dataclasses
import json
from datetime import datetime, timezone

import numpy as np
import pytest

import globalfeed.build as gb
from globalfeed import grib as gg
from globalfeed.latlon import to_feed, to_feed_float
from globalfeed.models import AIFS, GFS
from pipeline import blocks, thick
from pipeline.site import thick_path
from pipeline.squares import NX, NY, slice_square, square_name
from tests.gribfake import Files, msg

UTC = timezone.utc
RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 8, 5, 10, tzinfo=UTC)
G = 9.80665
# a 2 x 2 patch of the 0.25 degree grid at 40.25/40.0 N, 250.0/250.25 E (-110.0/-109.75)
PATCH = dict(lat_first=40.25, lat_last=40.0, lon_first=250.0, lon_last=250.25, step=0.25)


def m(values):
    return msg(values, **PATCH)


def fcst(fh):
    return "anl" if fh == 0 else f"{fh} hour fcst"


def serve(files, model, fh, levels, ice, water, decoy=True, adjacent=True):
    """A GFS file: cover, then ICMR over `levels`, then CLMR over `levels`.
    ice/water: {level: [4 values]}."""
    rows = [("PRES", "surface", fcst(fh), m([1, 2, 3, 4]))]
    for k, (n, lv) in enumerate(model.fields):
        rows.append((n, lv, fcst(fh), m([10 + k] * 4)))
    if decoy:
        rows.append(("ICMR", "100 mb", "0-6 hour ave fcst", m([9] * 4)))       # not the instantaneous one
        rows.append(("ICMR", "100 mb", "999 hour fcst", m([9] * 4)))
    if adjacent:
        for name, src in (("ICMR", ice), ("CLMR", water)):
            for lev in levels:
                rows.append((name, f"{lev} mb", fcst(fh), m(src[lev])))
    else:       # one level of each, then the next: no two wanted messages touch
        for lev in levels:
            rows.append(("ICMR", f"{lev} mb", fcst(fh), m(ice[lev])))
            rows.append(("CLMR", f"{lev} mb", fcst(fh), m(water[lev])))
    files.add(model.url(RUN, fh), rows)


LEVELS3 = (100, 150, 200)
THREE = dataclasses.replace(GFS, thick_levels=LEVELS3)


def test_thick_levels_on_models():
    assert GFS.thick_levels == (100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750,
                                800, 850, 900, 925, 950, 975, 1000)
    assert len(GFS.thick_levels) == 21
    assert AIFS.thick_levels == ()


def test_to_feed_float_keeps_units_and_gaps():
    lat, lon = np.array([40.0, 40.25]), np.array([-110.0, -110.0])
    out = to_feed_float(lat, lon, np.array([12.5, 700.0]))
    assert out.dtype == np.float64 and out.shape == (NY, NX)
    r, c = round((40.0 + 90) / 0.125), round((-110.0 + 180) / 0.125)
    assert out[r, c] == 12.5                       # not rounded, not clipped to 100
    assert out[r + 2, c] == 700.0
    assert np.isnan(out[0, 0])
    # to_feed (cover) is unchanged: same node, rounded and clipped
    cov = to_feed(lat, lon, np.array([12.5, 700.0]))
    assert (cov[r, c], cov[r + 2, c], cov[0, 0]) == (12, 100, 255)


def test_thick_ranges_exact_rows_and_anl():
    ice = {l: [0.001] * 4 for l in LEVELS3}
    for fh in (0, 6):
        f = Files()
        serve(f, THREE, fh, LEVELS3, ice, ice)
        idx = f.idx[THREE.index_url(RUN, fh)].decode()
        r = gg.thick_ranges(THREE, idx, fh)
        assert [lv for lv, _ in r["ICMR"]] == [100, 150, 200]
        rows = [ln.split(":") for ln in idx.strip().split("\n")]
        want = {int(x[1]) for x in rows if x[3] in ("ICMR", "CLMR") and x[5] == fcst(fh)}
        assert len(want) == 6                      # the two decoy rows are not among them
        assert {s for _, (s, _e) in r["ICMR"]} | {s for _, (s, _e) in r["CLMR"]} == want
    with pytest.raises(KeyError, match="ICMR"):
        gg.thick_ranges(THREE, idx, 7)


def test_thick_ranges_missing_level_raises():
    f = Files()
    ice = {l: [0.001] * 4 for l in (100, 150)}
    serve(f, THREE, 6, (100, 150), ice, ice)
    with pytest.raises(KeyError, match="200 mb"):
        gg.thick_ranges(THREE, f.idx[THREE.index_url(RUN, 6)].decode(), 6)


def test_split_messages():
    a, b, c = m([1, 2, 3, 4]), m([5, 6, 7, 8]), m([9, 9, 9, 9])
    assert gg.split_messages(a + b + c) == [a, b, c]
    assert gg.split_messages(a) == [a]


def test_read_thick_step_integral():
    # levels 100/150/200 mb: gradient of 10000/15000/20000 Pa = 5000 each, weight 5000 / 9.80665
    # = 509.8581 kg/m2 per kg/kg. At the first cell: ice (1 + 2 + 3) e-4, water (4 + 5 + 6) e-4.
    ice = {100: [1e-4] * 4, 150: [2e-4] * 4, 200: [3e-4] * 4}
    water = {100: [4e-4, 0, 0, 0], 150: [5e-4, 0, 0, 0], 200: [6e-4, 0, 0, 0]}
    f = Files()
    serve(f, THREE, 6, LEVELS3[::-1], ice, water)       # NCEP lists 1000 mb first: 200, 150, 100
    out = gg.read_thick_step(THREE, RUN, 6, f.get)
    assert len([c for c in f.calls if c[0] == THREE.url(RUN, 6)]) == 2
    assert set(out) == {"lat", "lon", "ice", "water"}
    assert out["ice"].tolist() == pytest.approx([6e-4 * 509.8581] * 4, rel=1e-5)
    assert float(np.sort(out["water"])[-1]) == pytest.approx(15e-4 * 509.8581, rel=1e-5)
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.75, -109.75]


def test_read_thick_step_one_request_per_adjacent_run():
    ice = {l: [1e-4] * 4 for l in LEVELS3}
    f = Files()
    serve(f, THREE, 6, LEVELS3, ice, ice, adjacent=True)
    gg.read_thick_step(THREE, RUN, 6, f.get)
    assert len([c for c in f.calls if c[0] == THREE.url(RUN, 6)]) == 2      # ICMR block, CLMR block
    f2 = Files()
    serve(f2, THREE, 6, LEVELS3, ice, ice, adjacent=False)
    gg.read_thick_step(THREE, RUN, 6, f2.get)
    assert len([c for c in f2.calls if c[0] == THREE.url(RUN, 6)]) == 6     # interleaved: one per message


def test_read_thick_step_nan_levels_add_nothing_and_all_nan_is_missing():
    # message order is the north row first: cells (40.25,-110) (40.25,-109.75) (40.0,-110) (40.0,-109.75)
    bad = -1e-6     # any negative reads as missing (kept small: a big one coarsens the 24-bit packing)
    ice = {100: [1e-4, 1e-4, bad, bad], 150: [1e-4, bad, bad, bad], 200: [1e-4, 1e-4, 1e-4, bad]}
    f = Files()
    serve(f, THREE, 6, LEVELS3, ice, ice)
    out = gg.read_thick_step(THREE, RUN, 6, f.get)
    got = {(round(la, 2), round(lo, 2)): v for la, lo, v in zip(out["lat"], out["lon"], out["ice"])}
    w = 5000 / G
    assert got[(40.25, -110.0)] == pytest.approx(3e-4 * w)      # all three levels
    assert got[(40.25, -109.75)] == pytest.approx(2e-4 * w)     # 150 mb missing: adds nothing
    assert got[(40.0, -110.0)] == pytest.approx(1e-4 * w)       # only 200 mb
    assert np.isnan(got[(40.0, -109.75)])                        # every level missing: missing


def test_read_thick_step_without_levels_raises():
    with pytest.raises(ValueError):
        gg.read_thick_step(AIFS, RUN, 6, lambda u, r=None: b"")


# ---- build ----------------------------------------------------------------

SQUARES = [square_name(40, -125), square_name(-5, 175), square_name(85, 0)]


def _small(model, main, tail):
    return dataclasses.replace(model, main_steps=tuple(main), tail_steps=tuple(tail))


def fake_values(fh):
    lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
    ice = (lat.ravel() + 90) / 360.0 * 0.5 + fh * 0.01        # kg/m2
    water = (lon.ravel() + 180) / 720.0 + fh * 0.002
    return lat.ravel(), lon.ravel(), ice, water


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(gb, "all_squares", lambda: list(SQUARES))
    monkeypatch.setattr(gb.history, "HEAD_STEPS", 6)

    def fake_read(model, run, fh, get):
        get(model.index_url(run, fh))
        lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
        out = {"lat": lat.ravel(), "lon": lon.ravel()}
        for i, f in enumerate(model.fields):
            k = f if isinstance(f, str) else f[0]
            out[k] = ((lat.ravel() + 90) / 1.8 + fh * 2 + i * 7) % 101
        return out

    def fake_thick(model, run, fh, get):
        get(model.index_url(run, fh))
        lat, lon, ice, water = fake_values(fh)
        return {"lat": lat, "lon": lon, "ice": ice, "water": water}

    monkeypatch.setattr(gb, "read_step", fake_read)
    monkeypatch.setattr(gb, "read_thick_step", fake_thick)
    return fake_thick


def _get(url, rng=None):
    return b""


def test_build_gfs_writes_thickness_over_main_steps_only(small, tmp_path):
    mod = _small(GFS, range(0, 3), (3, 6))
    state = tmp_path / "state"
    from datetime import timedelta
    from pipeline import history
    history.save_run_head(state, RUN - timedelta(hours=6), np.zeros((6, 4, NY, NX), dtype=np.uint8), sub=mod.name)
    gb.build(mod, RUN, tmp_path / "o", _get, NOW, state, sleep=lambda s: None)
    man = json.loads((tmp_path / "o" / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["historySteps"] == 6                       # the squares carry history ...
    assert man["thick"] == {"format": "ICT1", "run": "2026-11-08T00Z", "runMs": int(RUN.timestamp() * 1000),
                            "tiles": thick_path(RUN)}
    assert thick_path(RUN) == "thick/2026110800"
    for n in SQUARES:
        d = blocks.decode_blocks((tmp_path / "o" / thick_path(RUN) / f"{n}.ict").read_bytes(), 2)
        assert d["step_hours"] == [base, base + 1, base + 2]      # ... the thickness does not, and has no tail
        assert d["cube"].shape == (3, 2, 41, 41)
    # the cube is thick_bytes of the regridded kg/m2, no quantising
    n = SQUARES[0]
    d = blocks.decode_blocks((tmp_path / "o" / thick_path(RUN) / f"{n}.ict").read_bytes(), 2)
    lat, lon, ice, water = fake_values(1)
    want = np.stack([thick.thick_bytes(to_feed_float(lat, lon, ice)),
                     thick.thick_bytes(to_feed_float(lat, lon, water))])
    assert np.array_equal(d["cube"][1], slice_square(want, 40, -125))
    assert not (tmp_path / "o" / "thick" / "tail").exists()


def test_build_aifs_has_no_thickness(small, tmp_path):
    mod = _small(AIFS, (0, 6, 12), (18, 24))
    gb.build(mod, RUN, tmp_path, _get, NOW, tmp_path / "state", sleep=lambda s: None)
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert "thick" not in man
    assert not (tmp_path / "thick").exists()


def test_build_thickness_failure_publishes_cover_alone(small, tmp_path, monkeypatch, capsys):
    mod = _small(GFS, range(0, 4), (4,))

    def boom(model, run, fh, get):
        if fh == 2:
            raise OSError("pressure file missing")
        return small(model, run, fh, get)

    monkeypatch.setattr(gb, "read_thick_step", boom)
    gb.build(mod, RUN, tmp_path, _get, NOW, tmp_path / "state", sleep=lambda s: None)
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert "thick" not in man and man["blocks"] == "blocks/2026110800"
    assert not (tmp_path / "thick").exists()
    assert (tmp_path / "tiles" / "2026110800" / f"{SQUARES[0]}.icl").is_file()
    assert "::warning::thickness failed" in capsys.readouterr().out


def test_build_limited_steps(small, tmp_path):
    mod = _small(GFS, range(0, 12), (12,))
    gb.build(mod, RUN, tmp_path, _get, NOW, tmp_path / "state", sleep=lambda s: None, steps=[0, 6])
    man = json.loads((tmp_path / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["stepsMs"] == [base * 3_600_000, (base + 6) * 3_600_000]
    assert man["complete"] is False
    d = blocks.decode_blocks((tmp_path / thick_path(RUN) / f"{SQUARES[0]}.ict").read_bytes(), 2)
    assert d["step_hours"] == [base, base + 6]
