# IFS Direct: the model entry, the cover reader's unit check, thickness from
# column fields (tciw / tclw), 12-hourly runs with 6-hourly history, and one
# build end to end from real GRIB2 messages served with an ECMWF-style .index.
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import globalfeed.build as gb
from globalfeed import grib as gg
from globalfeed import history6
from globalfeed.models import AIFS, GFS, IFS
from pipeline import blocks, thick
from pipeline.encode import decode_square
from pipeline.site import blocks_path, tail_path, thick_path, tiles_path
from pipeline.squares import NX, NY, square_name
from tests.gribfake import Files, msg

UTC = timezone.utc
RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 8, 9, 10, tzinfo=UTC)
# A 2 x 2 patch at 40.25/40.0 N, -110.0/-109.75 E; message order is the north
# row first: (40.25,-110) (40.25,-109.75) (40.0,-110) (40.0,-109.75).
PATCH = dict(lat_first=40.25, lat_last=40.0, lon_first=-110.0, lon_last=-109.75, step=0.25)
PCT = {"lcc": 3073, "mcc": 3074, "hcc": 3075, "tcc": 228164}      # GRIB2 "%" encodings
FRAC = {"lcc": 186, "mcc": 187, "hcc": 188, "tcc": 164}           # ECMWF "(0 - 1)" encodings


def m(values, param):
    return msg(values, param=param, **PATCH)


def serve(files, model, fh, cover, ice=(0.01, 0.1, 1.0, 0.0), water=(0.2, 0.0, 0.0, 0.0),
          drop=(), ice_param="tciw"):
    """One IFS file: decoy pressure-level rows, the four cover fields
    ({key: (param, [4 values])}), then tciw and tclw."""
    rows = [("lcc", "pl", 500, m([99] * 4, PCT["lcc"])),
            ("tciw", "pl", 500, m([9.0] * 4, "tciw"))]
    for key in model.fields:
        param, vals = cover[key]
        rows.append((key, "sfc", m(vals, param)))
    if "tciw" not in drop:
        rows.append(("tciw", "sfc", m(list(ice), ice_param)))
    if "tclw" not in drop:
        rows.append(("tclw", "sfc", m(list(water), "tclw")))
    files.add_ecmwf(model.url(RUN, fh), model.index_url(RUN, fh), rows, step=str(fh))


def cover_of(enc, fh=0):
    scale = 0.01 if enc is FRAC else 1.0
    return {k: (enc[k], [(10 * (i + 1) + 2 * fh + j * 2) * scale for j in range(4)])
            for i, k in enumerate(IFS.fields)}


# ---- the model -------------------------------------------------------------

def test_ifs_model():
    assert IFS.name == "ifs"
    assert IFS.attribution == "Cloud data: © ECMWF, CC BY 4.0"
    assert IFS.fields == AIFS.fields == ("lcc", "mcc", "hcc", "tcc")
    assert IFS.main_steps == AIFS.main_steps == tuple(range(0, 121, 6))
    assert IFS.tail_steps == AIFS.tail_steps == tuple(range(126, 145, 6))
    assert IFS.index_kind == "ecmwf" and IFS.workers == 4
    assert IFS.run_hours == (0, 12) and IFS.run_spacing == 12
    assert IFS.thick_fields == (("tciw", "ice"), ("tclw", "water")) and IFS.thick_levels == ()
    assert IFS.has_thick and GFS.has_thick and not AIFS.has_thick
    assert IFS.check_cover_units and not GFS.check_cover_units and not AIFS.check_cover_units
    assert (GFS.run_hours, AIFS.run_hours) == ((0, 6, 12, 18),) * 2
    assert (GFS.run_spacing, AIFS.run_spacing) == (6, 6)
    run12 = datetime(2026, 11, 7, 12, tzinfo=UTC)
    assert IFS.url(run12, 6) == \
        "https://data.ecmwf.int/forecasts/20261107/12z/ifs/0p25/oper/20261107120000-6h-oper-fc.grib2"
    assert IFS.index_url(run12, 144) == \
        "https://data.ecmwf.int/forecasts/20261107/12z/ifs/0p25/oper/20261107120000-144h-oper-fc.index"


def test_model_refuses_both_thickness_sources():
    with pytest.raises(ValueError, match="exclusive"):
        dataclasses.replace(GFS, thick_fields=IFS.thick_fields)


def test_newest_complete_run_skips_06_and_18_without_asking():
    now = datetime(2026, 11, 8, 19, 10, tzinfo=UTC)            # cycles 18, 12, 06, 00
    asked = []

    def exists(u):
        asked.append(u)
        return u == IFS.index_url(datetime(2026, 11, 8, 0, tzinfo=UTC), 144)

    assert gb.newest_complete_run(IFS, exists, now) == datetime(2026, 11, 8, 0, tzinfo=UTC)
    assert asked == [IFS.index_url(datetime(2026, 11, 8, 12, tzinfo=UTC), 144),
                     IFS.index_url(datetime(2026, 11, 8, 0, tzinfo=UTC), 144)]
    assert gb.newest_complete_run(IFS, lambda u: False, now) is None


# ---- cover units -------------------------------------------------------------

def test_read_step_percent_kept():
    f = Files()
    serve(f, IFS, 6, cover_of(PCT, 6))
    out = gg.read_step(IFS, RUN, 6, f.get)
    for i, k in enumerate(IFS.fields):
        assert out[k].tolist() == pytest.approx([10 * (i + 1) + 12 + 2 * j for j in range(4)], abs=1e-4)
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.75, -109.75]
    # one index fetch, then one range per cover field: the pl decoys and the
    # thickness fields are never downloaded by the cover reader
    assert len(f.calls) == 1 + 4


def test_read_step_fraction_to_percent():
    f = Files()
    serve(f, IFS, 6, cover_of(FRAC, 6))
    out = gg.read_step(IFS, RUN, 6, f.get)
    for i, k in enumerate(IFS.fields):
        assert out[k].tolist() == pytest.approx([10 * (i + 1) + 12 + 2 * j for j in range(4)], abs=1e-3)


def test_read_step_mixed_encodings_decided_per_message():
    cov = cover_of(PCT, 0)
    cov["hcc"] = (FRAC["hcc"], [v / 100 for v in cov["hcc"][1]])
    f = Files()
    serve(f, IFS, 0, cov)
    out = gg.read_step(IFS, RUN, 0, f.get)
    assert out["hcc"].tolist() == pytest.approx([30, 32, 34, 36], abs=1e-3)
    assert out["lcc"].tolist() == pytest.approx([10, 12, 14, 16], abs=1e-4)


def test_read_step_refuses_other_units():
    cov = cover_of(PCT, 0)
    cov["mcc"] = ("2t", cov["mcc"][1])                        # kelvin
    f = Files()
    serve(f, IFS, 0, cov)
    with pytest.raises(ValueError, match="mcc.*units"):
        gg.read_step(IFS, RUN, 0, f.get)


# ---- thickness from column fields -------------------------------------------------

def test_read_thick_step_columns():
    f = Files()
    serve(f, IFS, 6, cover_of(PCT, 6), ice=(0.01, 0.1, 1.0, -1e-6), water=(0.2, 0.0, 0.5, 2.0))
    out = gg.read_thick_step(IFS, RUN, 6, f.get)
    assert set(out) == {"lat", "lon", "ice", "water"}
    got = {(round(la, 2), round(lo, 2)): (i, w)
           for la, lo, i, w in zip(out["lat"], out["lon"], out["ice"], out["water"])}
    assert got[(40.25, -110.0)] == pytest.approx((0.01, 0.2), abs=1e-6)
    assert got[(40.25, -109.75)] == pytest.approx((0.1, 0.0), abs=1e-6)
    assert got[(40.0, -110.0)] == pytest.approx((1.0, 0.5), abs=1e-6)
    ice, water = got[(40.0, -109.75)]
    assert np.isnan(ice) and water == pytest.approx(2.0, abs=1e-6)   # negative = missing
    data_calls = [c for c in f.calls if c[0] == IFS.url(RUN, 6)]
    assert len(f.calls) == 1 + 2 and len(data_calls) == 2
    # the sfc rows, never the pl decoy with the same param
    idx = [json.loads(ln) for ln in f.idx[IFS.index_url(RUN, 6)].decode().split("\n") if ln]
    want = {(r["_offset"], r["_offset"] + r["_length"] - 1)
            for r in idx if r["param"] in ("tciw", "tclw") and r["levtype"] == "sfc"}
    assert {c[1] for c in data_calls} == want


def test_read_thick_step_missing_field_raises():
    f = Files()
    serve(f, IFS, 6, cover_of(PCT, 6), drop=("tclw",))
    with pytest.raises(KeyError, match="tclw"):
        gg.read_thick_step(IFS, RUN, 6, f.get)


def test_read_thick_step_refuses_other_units():
    f = Files()
    serve(f, IFS, 6, cover_of(PCT, 6), ice_param=PCT["lcc"])   # a "%" message under the tciw row
    with pytest.raises(ValueError, match="tciw"):
        gg.read_thick_step(IFS, RUN, 6, f.get)


# ---- history: two runs a day, history 6 h apart -------------------------------------

def _head(*values):
    return np.stack([np.full((4, NY, NX), v, dtype=np.uint8) for v in values])


def test_history6_multi_step_heads_and_newest_wins(tmp_path):
    base = int(RUN.timestamp() // 3600)
    history6.save_run_head6(tmp_path, RUN - timedelta(hours=24), _head(1, 2))
    history6.save_run_head6(tmp_path, RUN - timedelta(hours=12), _head(3, 4))
    history6.save_run_head6(tmp_path, RUN - timedelta(hours=6), _head(5))   # covers -6 again, newer
    got = history6.load_history6(tmp_path, RUN)
    assert sorted(got) == [base - 24, base - 18, base - 12, base - 6]
    assert [int(got[h][0, 0, 0]) for h in sorted(got)] == [1, 2, 3, 5]
    with pytest.raises(ValueError):
        history6.save_run_head6(tmp_path, RUN, np.zeros((0, 4, NY, NX), dtype=np.uint8))


SQUARES = [square_name(40, -110), square_name(-5, 175)]


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(gb, "all_squares", lambda: list(SQUARES))

    def fake_read(model, run, fh, get):
        get(model.index_url(run, fh))
        lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
        out = {"lat": lat.ravel(), "lon": lon.ravel()}
        for i, k in enumerate(model.fields):
            out[k] = ((lat.ravel() + 90) / 1.8 + fh * 2 + i * 7) % 101
        return out

    def fake_thick(model, run, fh, get):
        get(model.index_url(run, fh))
        lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
        return {"lat": lat.ravel(), "lon": lon.ravel(),
                "ice": np.full(lat.size, 0.05 + fh * 0.01), "water": np.full(lat.size, 0.3)}

    monkeypatch.setattr(gb, "read_step", fake_read)
    monkeypatch.setattr(gb, "read_thick_step", fake_thick)


def test_build_ifs_history_from_12_hourly_runs(small, tmp_path):
    mod = dataclasses.replace(IFS, main_steps=(0, 6, 12), tail_steps=(18,))
    state = tmp_path / "state"
    history6.save_run_head6(state, RUN - timedelta(hours=24), _head(1, 2))
    history6.save_run_head6(state, RUN - timedelta(hours=12), _head(3, 4))
    history6.save_run_head6(state, RUN - timedelta(hours=72), _head(7, 8))   # too old
    gb.build(mod, RUN, tmp_path / "o", lambda u, r=None: b"", NOW, state, sleep=lambda s: None)
    man = json.loads((tmp_path / "o" / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["historySteps"] == 4
    assert man["stepsMs"] == [(base + h) * 3_600_000 for h in (-24, -18, -12, -6, 0, 6, 12)]
    assert man["model"] == "ifs" and man["attribution"] == "Cloud data: © ECMWF, CC BY 4.0"
    assert man["thick"]["format"] == "ICT1" and man["thick"]["tiles"] == thick_path(RUN)
    # the run saved f000 and f006 for the next one, 12 h later
    nxt = history6.load_history6(state, RUN + timedelta(hours=12))
    assert base in nxt and base + 6 in nxt
    with np.load(state / history6.SUB / "2026110800.npz") as z:
        assert z["head"].shape == (2, 4, NY, NX)


# ---- one build from real GRIB2 messages ----------------------------------------------------

def test_build_ifs_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(gb, "all_squares", lambda: [square_name(40, -110)])
    mod = dataclasses.replace(IFS, main_steps=(0, 6), tail_steps=(12,))
    f = Files()
    ices = {0: (0.01, 0.1, 1.0, -1e-6), 6: (0.02, 0.2, 2.0, -1e-6), 12: (0.0, 0.0, 0.0, 0.0)}
    for fh in (0, 6, 12):
        serve(f, mod, fh, cover_of(FRAC if fh == 6 else PCT, fh), ice=ices[fh])
    gb.build(mod, RUN, tmp_path, f.get, NOW, tmp_path / "state", sleep=lambda s: None)
    man = json.loads((tmp_path / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["model"] == "ifs" and man["complete"] is True and man["historySteps"] == 0
    assert man["stepsMs"] == [base * 3_600_000, (base + 6) * 3_600_000]
    assert man["tail"]["stepsMs"] == [(base + 12) * 3_600_000] and man["tail"]["tiles"] == tail_path(RUN)
    assert man["thick"] == {"format": "ICT1", "run": "2026-11-08T00Z", "runMs": int(RUN.timestamp() * 1000),
                            "tiles": thick_path(RUN)}
    n = square_name(40, -110)
    icl = decode_square((tmp_path / tiles_path(RUN) / f"{n}.icl").read_bytes())
    icb = blocks.decode_blocks((tmp_path / blocks_path(RUN) / f"{n}.icb").read_bytes(), 4)
    assert np.array_equal(icl["cube"], icb["cube"])
    # square rows go south to north from 40.0, columns west to east from -110.0:
    # node (0,0) = (40.0,-110) = 3rd value; (2,2) = (40.25,-109.75) = 2nd; (2,0) = 1st
    for s, fh in enumerate((0, 6)):            # f006 arrived as (0 - 1) and reads the same as %
        for i in range(4):
            v = [10 * (i + 1) + 2 * fh + 2 * j for j in range(4)]
            c = icl["cube"][s, i]
            assert (c[0, 0], c[2, 2], c[2, 0], c[0, 2]) == (v[2], v[1], v[0], v[3])
            assert c[40, 40] == 255                      # outside the patch: no data
    ict = blocks.decode_blocks((tmp_path / thick_path(RUN) / f"{n}.ict").read_bytes(), 2)
    assert ict["step_hours"] == [base, base + 6]
    for s, fh in enumerate((0, 6)):
        ice = ict["cube"][s, 0]
        want = thick.thick_bytes(np.array(ices[fh][:3]))
        assert (ice[2, 0], ice[2, 2], ice[0, 0]) == tuple(want)
        assert ice[0, 2] == thick.MISSING                # negative tciw: missing
        assert ict["cube"][s, 1][2, 0] == thick.thick_bytes(np.array([0.2]))[0]
    # history saved for the next run: f000 and f006
    assert sorted(history6.load_history6(tmp_path / "state", RUN + timedelta(hours=12))) == [base, base + 6]
