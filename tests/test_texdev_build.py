import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
import pytest

import run_build
import texdev_build
from pipeline import clues, columns, dwd
from pipeline.cluefile import decode_clue_square
from pipeline.columns import decode_columns, dequantize, MAGIC_UPPER
from pipeline.history import HEAD_STEPS, hour_of
from pipeline.squares import NX, NY, square_name
from tests.test_run_build import _column_value, _listing

UTC = dt.timezone.utc
RUN = dt.datetime(2026, 11, 12, 6, tzinfo=UTC)
G = 9.80665


def _std_gh(p_hpa):
    """A standard-atmosphere height (m) for a pressure level."""
    return 44330.8 * (1.0 - (p_hpa / 1013.25) ** 0.190263)


# Fake upper-air values in DWD's units (K, %, m² s⁻², m/s, J/kg, m).
SINGLES = {"CAPE_CON": 120.0, "CAPE_ML": 300.0, "HBAS_CON": 1500.0, "HTOP_CON": 6000.0}


def _upper_value(name, fi_scale=G):
    m = re.search(r"_pressure-level_\d{10}_(\d{3})_(\d+)_([A-Z]+)\.", name)
    if m and m.group(3) in ("U", "V"):
        # Wind at every level, the ICC1 levels included; it turns with height
        # so the low deck has shear.
        step, lv = int(m.group(1)), int(m.group(2))
        return lv, (2.0 + (1000 - lv) / 20 + 0.1 * step if m.group(3) == "U" else -3.0 + (1000 - lv) / 100)
    if m and int(m.group(2)) in dwd.UPPER_PLEVELS:
        step, lv, f = int(m.group(1)), int(m.group(2)), m.group(3)
        gh = _std_gh(lv)
        return lv, {"T": 288.15 - 0.0065 * gh + 0.1 * step, "RELHUM": 40.0 + lv / 20,
                    "FI": fi_scale * gh}[f]
    m = re.search(r"_single-level_\d{10}_(\d{3})_([A-Z_]+)\.", name)
    if m and m.group(2) in SINGLES:
        return None, SINGLES[m.group(2)]
    return None


def _fake_lattice(fi_scale=G):
    def lattice_levels(paths, kit, work, stride, nearest=False):
        shape = columns.lattice(np.zeros((NY, NX), np.uint8), stride).shape
        out = {}
        for p in paths:
            # WW (categorical) is fetched nearest-link; every other field, the
            # upper air included, is blended.
            assert nearest == ("_WW." in Path(p).name), (Path(p).name, nearest)
            got = _upper_value(Path(p).name, fi_scale) or _column_value(Path(p).name)
            out[got[0]] = np.full(shape, got[1], dtype=np.float32)
        return out
    return lattice_levels


def _deps(run=RUN, fi_scale=G, steps=(0, 1)):
    files = dwd.column_files(run, steps)
    for f, names in dwd.upper_files(run, steps).items():
        files.setdefault(f, []).extend(names)

    def listing(hh, field):
        if hh != f"{run:%H}":
            return ""
        if field in dwd.FIELDS:
            return _listing(run, field)
        return "".join(f'<a href="{n}">x</a>\n' for n in files.get(field, []))

    return run_build.Deps(listing=listing, fetch_text=None, download=lambda url, dst: Path(dst).write_bytes(b"x"),
                          regrid=None, ensure_kit=lambda work: Path("/kit"),
                          now=lambda: run + dt.timedelta(hours=3), lattice_levels=_fake_lattice(fi_scale),
                          sleep=lambda s: None, validate=lambda world: [])


def _build(tmp_path, deps, steps=(0, 1), out="out"):
    return texdev_build.build(tmp_path / out, tmp_path / "state", tmp_path / "work", deps,
                              steps=list(steps), workers=2)


def test_texdev_squares_cover_the_us_and_iceland():
    names = set(texdev_build.TEXDEV_SQUARES)
    assert len(texdev_build.TEXDEV_SQUARES) == len(names) == 293
    # The twelve US-West squares every test case sits in are still there.
    assert {square_name(lat, lon) for lat in (35, 40, 45) for lon in (-130, -125, -120, -115)} <= names
    # A place in each part, and the sea one square off each coast.
    for lat, lon in ((40, -75), (25, -85), (45, -70),       # New York, Florida, Maine
                     (60, -150), (70, -160), (50, -180),    # Anchorage, the North Slope, the Aleutians
                     (20, -160), (15, -160),                # Oahu, Hawaii island
                     (60, -25), (65, -15),                  # Reykjavik, east Iceland
                     (35, -130), (30, -65), (55, -30)):     # Pacific, Atlantic, the sea south-west of Iceland
        assert square_name(lat, lon) in names
    # Reykjavik's window (about 830 km each way) and Seattle's and Maine's reach these.
    for lat, lon in ((60, -35), (65, -35), (65, -45), (60, 0), (45, -140), (45, -60), (70, -115), (75, -150)):
        assert square_name(lat, lon) in names
    # Nothing past the dateline, nothing in the southern hemisphere, nothing east of 5 E.
    for n in names:
        assert n[0] == "N" and ("W" in n or n.endswith("E000"))


def test_upper_keys_follow_the_icu1_field_table():
    assert run_build.upper_keys()[:2 * len(dwd.COLUMN_PLEVELS)] == \
        [(f, p) for p in dwd.COLUMN_PLEVELS for f in ("U", "V")]
    spec = run_build.upper_spec()
    keys = run_build.upper_keys()
    assert len(spec) == len(keys)
    kind = {"T": columns.T, "RELHUM": columns.RH, "FI": columns.GH, "U": columns.U, "V": columns.V,
            "CAPE_CON": columns.CAPE_CON, "CAPE_ML": columns.CAPE_ML,
            "HBAS_CON": columns.HBAS_CON, "HTOP_CON": columns.HTOP_CON}
    for (field, lv), f in zip(keys, spec):
        assert f[0] == kind[field]
        assert f[2] == (lv or 0)


def test_the_manifest_names_upper_and_clues(tmp_path):
    steps = [0, 1]
    assert _build(tmp_path, _deps(steps=steps), steps) == run_build.BUILT
    root = tmp_path / "out" / "texdev"
    m = json.loads((root / "manifest.json").read_text())
    hours = [hour_of(RUN) + s for s in steps]
    for key, fmt, ext in (("upper", "ICU1", "icu"), ("clues", "ICX1", "icx")):
        e = m[key]
        assert {"format", "run", "runMs", "steps", "tiles"} <= set(e)
        assert e["format"] == fmt
        assert e["run"] == dwd.run_iso(RUN)
        assert e["runMs"] == int(RUN.timestamp() * 1000)
        assert e["steps"] == [h * 3600 * 1000 for h in hours]
        assert e["tiles"] == f"{key}/{dwd.run_id(RUN)}"
        assert sorted(p.stem for p in (root / e["tiles"]).glob(f"*.{ext}")) == sorted(texdev_build.TEXDEV_SQUARES)
    assert sorted(m["squares"]) == sorted(texdev_build.TEXDEV_SQUARES)
    # Only the texdev files: never a column (ICC1) square, never the live paths.
    assert sorted(p.name for p in root.iterdir()) == ["clues", "manifest.json", "upper"]
    assert not list((tmp_path / "out").glob("**/*.icc"))


def test_upper_air_is_in_app_units(tmp_path):
    steps = [0, 1]
    _build(tmp_path, _deps(steps=steps), steps)
    root = tmp_path / "out" / "texdev"
    d = decode_columns((root / f"upper/{dwd.run_id(RUN)}/N40W125.icu").read_bytes(), magic=MAGIC_UPPER)
    assert [f[:3] for f in d["spec"]] == [tuple(f[:3]) for f in run_build.upper_spec()]
    by = {(f[0], f[2]): (codes, f) for f, codes in zip(d["spec"], d["fields"])}

    def val(kind, lv, s=0):
        codes, f = by[(kind, lv)]
        return float(dequantize(codes[0, 0, s], f[3], f[4]))

    for lv in dwd.UPPER_PLEVELS:
        assert val(columns.GH, lv) == pytest.approx(_std_gh(lv), abs=0.5 + 1e-3)
        assert val(columns.T, lv, s=1) == pytest.approx(288.15 - 0.0065 * _std_gh(lv) + 0.1 - 273.15, abs=0.05 + 1e-4)
    for lv in dwd.COLUMN_PLEVELS + dwd.UPPER_PLEVELS:
        assert val(columns.U, lv) == pytest.approx(2.0 + (1000 - lv) / 20, abs=0.05 + 1e-4)
        assert val(columns.V, lv) == pytest.approx(-3.0 + (1000 - lv) / 100, abs=0.05 + 1e-4)
    assert val(columns.HBAS_CON, 0) == pytest.approx(SINGLES["HBAS_CON"], abs=0.5)
    # The ICC1 levels' T/RH/GH stay in ICC1: ICU1 carries only their wind.
    for lv in dwd.COLUMN_PLEVELS:
        for kind in (columns.T, columns.RH, columns.GH):
            assert (kind, lv) not in by


def test_upper_files_fetch_wind_at_the_column_levels_and_nothing_else_there():
    files = dwd.upper_files(RUN, [0])
    for f in ("U", "V"):
        assert files[f] == [dwd.pressure_file_name(RUN, 0, lv, f) for lv in dwd.COLUMN_PLEVELS + dwd.UPPER_PLEVELS]
    for f in ("T", "RELHUM", "FI"):
        assert files[f] == [dwd.pressure_file_name(RUN, 0, lv, f) for lv in dwd.UPPER_PLEVELS]


def _profile(col_vals, up_vals):
    """A clue profile from app-unit values, built independently of run_build."""
    p, t, rh, gh, u, v = [], [], [], [], [], []
    for lv in dwd.COLUMN_PLEVELS:
        p.append(lv); t.append(col_vals[("T", lv)]); rh.append(col_vals[("RH", lv)]); gh.append(col_vals[("GH", lv)])
        u.append(up_vals[("U", lv)]); v.append(up_vals[("V", lv)])
    for lv in dwd.UPPER_PLEVELS:
        p.append(lv); t.append(up_vals[("T", lv)]); rh.append(up_vals[("RH", lv)]); gh.append(up_vals[("GH", lv)])
        u.append(up_vals[("U", lv)]); v.append(up_vals[("V", lv)])
    out = dict(p=p, t=t, rh=rh, gh=gh, u=u, v=v)
    out.update(cape_con=up_vals["CAPE_CON"], cape_ml=up_vals["CAPE_ML"],
               hbas_con=up_vals["HBAS_CON"], htop_con=up_vals["HTOP_CON"])
    return out


def _q(v, kind):
    sc, off = columns._QUANT[kind]
    return float(dequantize(columns.quantize(np.atleast_1d(v), sc, off), sc, off)[0])


def test_clue_bytes_match_encode_clues_on_the_node_profile(tmp_path):
    steps = [0, 1]
    _build(tmp_path, _deps(steps=steps), steps)
    root = tmp_path / "out" / "texdev"
    d = decode_clue_square((root / f"clues/{dwd.run_id(RUN)}/N40W125.icx").read_bytes())
    assert d["step_hours"] == [hour_of(RUN) + s for s in steps]
    assert d["clues"].shape == (2, 21, 21, clues.N_BYTES)
    for s in steps:
        col, up = {}, {}
        for lv in dwd.COLUMN_PLEVELS:
            for name, kind, key in (("T", columns.T, "T"), ("RELHUM", columns.RH, "RH"), ("FI", columns.GH, "GH")):
                _, raw = _column_value(dwd.pressure_file_name(RUN, s, lv, name))
                raw = np.float32(raw)
                col[(key, lv)] = _q(raw - 273.15 if name == "T" else (raw / G if name == "FI" else raw), kind)
            for name, kind in (("U", columns.U), ("V", columns.V)):
                _, raw = _upper_value(dwd.pressure_file_name(RUN, s, lv, name))
                up[(name, lv)] = _q(np.float32(raw), kind)
        for lv in dwd.UPPER_PLEVELS:
            for name, kind, key in (("T", columns.T, "T"), ("RELHUM", columns.RH, "RH"), ("FI", columns.GH, "GH"),
                                    ("U", columns.U, "U"), ("V", columns.V, "V")):
                _, raw = _upper_value(dwd.pressure_file_name(RUN, s, lv, name))
                raw = np.float32(raw)
                up[(key, lv)] = _q(raw - 273.15 if name == "T" else (raw / G if name == "FI" else raw), kind)
        for f, kind in (("CAPE_CON", columns.CAPE_CON), ("CAPE_ML", columns.CAPE_ML),
                        ("HBAS_CON", columns.HBAS_CON), ("HTOP_CON", columns.HTOP_CON)):
            up[f] = _q(np.float32(SINGLES[f]), kind)
        prof = _profile(col, up)
        want = clues.encode_clues(clues.deck_clues(prof), clues.node_clues(prof))
        assert bytes(d["clues"][s, 3, 7]) == want
    # The fake air is moist enough at 600-200 hPa that the mid deck is not all missing.
    assert (d["clues"][0, 0, 0, 7:14] != clues.MISSING).any()
    # With wind at 1000-700 hPa the low deck has wind, shear and its direction.
    low = clues.decode_clues(bytes(d["clues"][0, 3, 7]))["decks"]["low"]
    assert low["wind"] is not None and low["shear"] is not None and low["shearDir"] is not None


def test_fi_already_in_metres_fails_the_build_not_clips(tmp_path, capsys):
    steps = [0, 1]
    assert _build(tmp_path, _deps(fi_scale=1.0, steps=steps), steps) == run_build.FAILED
    out = capsys.readouterr().out
    assert "GH" in out and "3000" in out
    assert not (tmp_path / "out" / "texdev" / "manifest.json").exists()


def test_upper_history_is_kept_and_read_back(tmp_path):
    steps = list(range(HEAD_STEPS))
    assert _build(tmp_path, _deps(steps=steps), steps) == run_build.BUILT
    assert len(list((tmp_path / "state" / "upper").glob(f"{dwd.run_id(RUN)}.h*.npz"))) == HEAD_STEPS
    nxt = RUN + dt.timedelta(hours=6)
    assert texdev_build.build(tmp_path / "out2", tmp_path / "state", tmp_path / "work", _deps(run=nxt, steps=[0, 1]),
                              steps=[0, 1], workers=2) == run_build.BUILT
    m = json.loads((tmp_path / "out2" / "texdev" / "manifest.json").read_text())
    want = [(hour_of(RUN) + s) * 3600 * 1000 for s in range(HEAD_STEPS)] + \
           [(hour_of(nxt) + s) * 3600 * 1000 for s in (0, 1)]
    assert m["upper"]["steps"] == want
    assert m["upper"]["historySteps"] == HEAD_STEPS
    assert m["clues"]["steps"] == want
    d = decode_clue_square((tmp_path / "out2" / "texdev" / m["clues"]["tiles"] / "N40W125.icx").read_bytes())
    # The history hours' clues are rebuilt from the upper history (and the
    # column history the first build kept), not left missing.
    assert d["clues"].shape[0] == len(want)
    first = decode_clue_square((tmp_path / "out" / "texdev" / f"clues/{dwd.run_id(RUN)}/N40W125.icx").read_bytes())
    assert np.array_equal(d["clues"][:HEAD_STEPS], first["clues"])


def test_a_live_build_writes_no_upper_or_clues(tmp_path):
    # build() is unchanged: the texdev files come only from texdev_build.
    import inspect
    assert "build_upper_and_clues" not in inspect.getsource(run_build.build)
