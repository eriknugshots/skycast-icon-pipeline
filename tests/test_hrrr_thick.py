import json
from datetime import datetime, timezone

import numpy as np
import pytest

import hrrr.build as hb
import hrrr.grib as hg
from hrrr.grid import SQUARES, bin_index, box_mean
from pipeline import blocks, thick
from pipeline.site import thick_path, tiles_path
from pipeline.squares import square_name
from tests.gribfake import Files, msg

UTC = timezone.utc
RUN = datetime(2026, 11, 6, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
COVER = [("LCDC", "low cloud layer"), ("MCDC", "middle cloud layer"),
         ("HCDC", "high cloud layer"), ("TCDC", "entire atmosphere")]


def fcst(fh):
    return "anl" if fh == 0 else f"{fh} hour fcst"


def prs_url(fh):
    return hg.file_url(RUN, fh).replace("wrfsfc", "wrfprs")


def serve(files, fh, ice=(0.02, 0.04, 0.06, 0.08), water=(0.1, 0.2, 0.3, 0.4), snmr=1e-4, thick_rows=True):
    """One forecast hour of both HRRR files. Every SNMR level carries `snmr` everywhere."""
    rows = [("PRES", "surface", fcst(fh), msg([1, 2, 3, 4]))]
    for k, (n, lv) in enumerate(COVER):
        rows.append((n, lv, fcst(fh), msg([10 + k, 20 + k, 30 + k, 40 + k])))
    if thick_rows:
        rows.append(("TCOLW", "entire atmosphere (considered as a single layer)", fcst(fh), msg(water)))
        rows.append(("TCOLI", "entire atmosphere (considered as a single layer)", fcst(fh), msg(ice)))
    files.add(hg.file_url(RUN, fh), rows)
    prs = [("TMP", f"{p} mb", fcst(fh), msg([250] * 4)) for p in (100, 125)]
    for p in range(100, 1001, 25):      # HRRR's own 25 mb spacing; the reader keeps every other one
        prs.append(("SNMR", f"{p} mb", fcst(fh), msg([snmr] * 4)))
    prs.append(("RH", "1000 mb", fcst(fh), msg([50] * 4)))
    files.add(prs_url(fh), prs)


def test_layer_weights_three_levels():
    # levels 100, 150, 200 hPa -> 10000, 15000, 20000 Pa; np.gradient of those = 5000, 5000, 5000;
    # each weight is dp / g, g = 9.80665.
    w = hg.layer_weights([100, 150, 200])
    assert w.tolist() == pytest.approx([509.8581, 509.8581, 509.8581], abs=1e-4)
    # the same three levels carrying 1, 2, 3 g/kg: (1 + 2 + 3) e-3 * 509.8581 = 3.0591 kg/m2
    assert float(np.dot(w, [1e-3, 2e-3, 3e-3])) == pytest.approx(3.0591, abs=1e-4)


def test_layer_weights_uneven_levels():
    # levels 100, 150, 250 hPa: gradient = (5000, 7500, 10000) Pa
    w = hg.layer_weights([100, 150, 250])
    assert (w * hg.G).tolist() == pytest.approx([5000, 7500, 10000])


def test_thick_ranges_picks_the_right_rows():
    f = Files()
    serve(f, 5)
    sfc = f.idx[hg.file_url(RUN, 5) + ".idx"].decode()
    prs = f.idx[prs_url(5) + ".idx"].decode()
    r = hg.thick_ranges(sfc, prs, 5)
    assert set(r) == {"TCOLI", "TCOLW", "SNMR"}
    assert [lv for lv, _ in r["SNMR"]] == list(range(100, 1001, 50))     # every other level from 100 mb
    assert len(r["SNMR"]) == 19


def test_thick_ranges_f000_is_anl_and_other_hours_do_not_match():
    f = Files()
    serve(f, 0)
    assert hg.thick_ranges(f.idx[hg.file_url(RUN, 0) + ".idx"].decode(), f.idx[prs_url(0) + ".idx"].decode(), 0)
    with pytest.raises(KeyError):
        hg.thick_ranges(f.idx[hg.file_url(RUN, 0) + ".idx"].decode(), f.idx[prs_url(0) + ".idx"].decode(), 3)


def test_thick_ranges_missing_field_raises():
    f = Files()
    serve(f, 5, thick_rows=False)
    with pytest.raises(KeyError, match="TCOLI"):
        hg.thick_ranges(f.idx[hg.file_url(RUN, 5) + ".idx"].decode(), f.idx[prs_url(5) + ".idx"].decode(), 5)


def test_read_thick_ice_is_tcoli_plus_snow():
    f = Files()
    serve(f, 5, snmr=1e-4)
    out = hg.read_thick(RUN, 5, f.get)
    assert set(out) == {"ice", "water"}
    # 19 levels 100..1000 mb at 50 mb: gradient = 5000 Pa at both ends and 5000 between, so each
    # level weighs 5000 / 9.80665 kg/m2 per kg/kg; 19 * 1e-4 * 509.8581 = 0.96873 kg/m2 of snow.
    snow = 19 * 1e-4 * 5000 / hg.G
    assert snow == pytest.approx(0.968730, abs=1e-6)
    got = sorted(out["ice"].tolist())
    assert got == pytest.approx([0.02 + snow, 0.04 + snow, 0.06 + snow, 0.08 + snow], abs=1e-5)
    assert sorted(out["water"].tolist()) == pytest.approx([0.1, 0.2, 0.3, 0.4], abs=1e-6)


def test_read_thick_asks_for_the_prs_file_only_for_snow():
    f = Files()
    serve(f, 5)
    hg.read_thick(RUN, 5, f.get)
    urls = [u for u, _ in f.calls]
    assert urls[0] == hg.file_url(RUN, 5) + ".idx" and urls[1] == prs_url(5) + ".idx"
    assert sum(1 for u in urls if u == prs_url(5) and True) == 19


def test_read_thick_nan_tcoli_stays_missing():
    f = Files()
    serve(f, 5, ice=(0.02, -9999.0, 0.06, 0.08))     # negative reads as missing, as in read_fields
    out = hg.read_thick(RUN, 5, f.get)
    assert int(np.isnan(out["ice"]).sum()) == 1
    assert int(np.isnan(out["water"]).sum()) == 0


def test_box_mean_means_nan_gaps():
    lat = np.array([40.0, 40.03, 40.0])
    lon = np.array([-110.0, -110.03, -109.0])
    vals = np.array([1.0, 3.0, np.nan])
    out = box_mean(vals, bin_index(lat, lon))["N40W110"]
    assert out.dtype == np.float64
    assert out[0, 0] == pytest.approx(2.0)
    # the NaN cell votes for nothing: its node (col 8) is NaN, not 0
    assert np.isnan(out[0, 8])
    assert np.isnan(out[5, 5])


def _build(tmp_path, files, hours=2, monkeypatch=None):
    monkeypatch.setattr(hb, "last_hour", lambda run: hours)
    return hb.build(RUN, tmp_path, files.get, NOW, sleep=lambda s: None)


def _files(hours=2, **kw):
    f = Files()
    for fh in range(hours + 1):
        serve(f, fh, **kw)
    return f


def test_build_writes_thickness_and_names_it(tmp_path, monkeypatch):
    f = _files(2)
    _build(tmp_path, f, 2, monkeypatch)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["model"] == "hrrr"
    assert m["thick"] == {"format": "ICT1", "run": "2026-11-06T12Z", "runMs": int(RUN.timestamp() * 1000),
                          "tiles": thick_path(RUN)}
    assert thick_path(RUN) == "thick/2026110612"
    for k in ("blocks", "columns", "tail"):
        assert k not in m
    for sl, so in SQUARES:
        assert (tmp_path / thick_path(RUN) / f"{square_name(sl, so)}.ict").is_file()
    base = int(RUN.timestamp() // 3600)
    d = blocks.decode_blocks((tmp_path / thick_path(RUN) / "N40W110.ict").read_bytes(), 2)
    assert d["step_hours"] == [base, base + 1, base + 2]
    assert d["cube"].shape == (3, 2, 41, 41)
    # ice byte = thick_bytes(TCOLI + snow), water byte = thick_bytes(TCOLW)
    snow = 19 * 1e-4 * 5000 / hg.G
    want_ice = {int(thick.thick_bytes(v + snow)) for v in (0.02, 0.04, 0.06, 0.08)}
    want_water = {int(thick.thick_bytes(v)) for v in (0.1, 0.2, 0.3, 0.4)}
    assert {int(v) for v in d["cube"][0, 0].ravel() if v != 255} == want_ice
    assert {int(v) for v in d["cube"][0, 1].ravel() if v != 255} == want_water
    # a node no source cell voted for is 255
    assert (d["cube"][0, :, 30, 30] == 255).all()
    # a far square with no data at all is 255 throughout
    far = blocks.decode_blocks((tmp_path / thick_path(RUN) / f"{square_name(*SQUARES[0])}.ict").read_bytes(), 2)
    assert (far["cube"] == 255).all()


def test_build_without_thickness_rows_publishes_cover_alone(tmp_path, monkeypatch, capsys):
    f = _files(2, thick_rows=False)
    _build(tmp_path, f, 2, monkeypatch)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert "thick" not in m
    assert not (tmp_path / "thick").exists()
    assert (tmp_path / tiles_path(RUN) / "N40W110.icl").is_file()
    assert "::warning::thickness failed" in capsys.readouterr().out


def test_build_one_bad_thickness_hour_drops_all_thickness_not_the_build(tmp_path, monkeypatch):
    f = _files(2)
    real = f.get
    seen = {"n": 0}

    def get(url, rng=None):
        if url == prs_url(1) and rng is not None:
            seen["n"] += 1
            raise OSError("prs gone")
        return real(url, rng)

    monkeypatch.setattr(hb, "last_hour", lambda run: 2)
    hb.build(RUN, tmp_path, get, NOW, sleep=lambda s: None)
    assert seen["n"] >= 3                       # retried before giving up
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert "thick" not in m
    assert not (tmp_path / "thick").exists()
    assert (tmp_path / tiles_path(RUN) / "N40W110.icl").is_file()


def test_build_limited_hours(tmp_path, monkeypatch):
    f = _files(6)
    monkeypatch.setattr(hb, "last_hour", lambda run: 6)
    hb.build(RUN, tmp_path, f.get, NOW, sleep=lambda s: None, fhs=[0, 3])
    m = json.loads((tmp_path / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert m["stepsMs"] == [base * 3_600_000, (base + 3) * 3_600_000]
    assert m["complete"] is False
    d = blocks.decode_blocks((tmp_path / thick_path(RUN) / "N40W110.ict").read_bytes(), 2)
    assert d["step_hours"] == [base, base + 3]


def test_parse_hours():
    import hrrr_build
    assert hrrr_build.parse_hours("0,3,6-9") == [0, 3, 6, 7, 8, 9]
    assert hrrr_build.parse_hours(None) is None


def test_cli_hours_option_builds_a_sample(tmp_path, monkeypatch):
    import hrrr_build
    f = _files(6)
    monkeypatch.setattr(hb, "last_hour", lambda run: 6)
    monkeypatch.setattr(hrrr_build, "_now", lambda: NOW)
    monkeypatch.setattr(hrrr_build, "_exists", lambda u: u == hg.file_url(RUN, 6) + ".idx")
    monkeypatch.setattr(hrrr_build, "_get", f.get)
    assert hrrr_build.main(["--out", str(tmp_path), "--hours", "0,3"]) == 0
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert len(m["stepsMs"]) == 2 and m["complete"] is False and "thick" in m
