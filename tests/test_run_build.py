import datetime as dt
import json
import numpy as np
from pathlib import Path
from pipeline.dwd import STEPS, FIELDS, TAIL_STEPS, file_name
from pipeline.encode import decode_square
from pipeline.history import save_run_head, hour_of
from pipeline.squares import NX, NY
import pytest
import run_build

# A full build publishes every square on the globe; these tests use nine.
NINE = ["N35W120", "N35W125", "N35W130", "N40W120", "N40W125", "N40W130", "N45W120", "N45W125", "N45W130"]


@pytest.fixture(autouse=True)
def _nine_squares(monkeypatch):
    monkeypatch.setattr(run_build, "site_squares", lambda: list(NINE))

UTC = dt.timezone.utc
RUN = dt.datetime(2026, 9, 27, 0, tzinfo=UTC)


def _listing(run, field, steps=STEPS):
    return "".join(f'<a href="{file_name(run, s, field)}">x</a>\n' for s in steps)


def _no_columns(paths, kit, work, stride):
    raise AssertionError("these listings carry no column files: no column build")


def _fake_deps(tmp_path, world_value=lambda step, field: 0, prev_manifest=None):
    calls = {"downloads": [], "regrids": [], "sleeps": []}

    def listing(hh, field):
        return _listing(RUN, field) if hh == "00" else ""

    def fetch_text(url):
        assert url.endswith("manifest.json")
        if prev_manifest is None:
            raise OSError("404")
        return json.dumps(prev_manifest)

    def download(url, dst):
        calls["downloads"].append(url)
        Path(dst).write_bytes(b"fake")

    def regrid(grib_bz2, kit, work, scale=1.0):
        name = Path(grib_bz2).name
        step = int(name.split("_")[-2]); field = name.split("_")[-1].split(".")[0]
        calls["regrids"].append((step, field))
        return np.full((NY, NX), world_value(step, field), dtype=np.uint8)

    return run_build.Deps(listing=listing, fetch_text=fetch_text, download=download,
                          regrid=regrid, ensure_kit=lambda work: Path("/kit"),
                          now=lambda: dt.datetime(2026, 9, 27, 3, tzinfo=UTC),
                          lattice_levels=_no_columns, sleep=lambda s: calls["sleeps"].append(s)), calls


def test_early_exit_when_the_site_already_has_this_run(tmp_path, capsys):
    deps, calls = _fake_deps(tmp_path, prev_manifest={"run": "2026-09-27T00Z", "complete": True})
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=None, squares=None)
    assert rc == run_build.NOTHING_TO_DO
    assert calls["downloads"] == []
    assert not (tmp_path / "site").exists()


def test_a_build_writes_squares_manifest_and_state(tmp_path):
    deps, calls = _fake_deps(tmp_path, world_value=lambda step, field: min(100, step + FIELDS.index(field)))
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=[0, 1, 2, 3, 4, 5, 6], squares=["N40W125"])
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["run"] == "2026-09-27T00Z" and m["squares"] == ["N40W125"]
    assert m["stepsMs"] == [(hour_of(RUN) + s) * 3600000 for s in range(7)]
    assert m["historySteps"] == 0
    sq = decode_square((tmp_path / "site" / "tiles" / "2026092700" / "N40W125.icl").read_bytes())
    assert sq["step_hours"] == [hour_of(RUN) + s for s in range(7)]
    assert sq["cube"].shape == (7, 4, 41, 41)
    assert sq["cube"][6, 2, 0, 0] == 6 + 2                # step 6, field CLCH
    assert sorted(calls["regrids"])[:2] == [(0, "CLCH"), (0, "CLCL")]
    assert len(calls["downloads"]) == 7 * 4
    # The run's hours 0-5 are saved for the next build's history.
    assert (tmp_path / "state" / "history" / "2026092700.npz").exists()


def test_history_from_state_is_prepended(tmp_path):
    prev = RUN - dt.timedelta(hours=6)
    save_run_head(tmp_path / "state", prev, np.full((6, 4, NY, NX), 42, dtype=np.uint8))
    deps, _ = _fake_deps(tmp_path)
    run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                    deps=deps, pages_base="https://x", steps=[0, 1], squares=["N00E000"])
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["historySteps"] == 6
    assert m["stepsMs"][0] == hour_of(prev) * 3600000
    sq = decode_square((tmp_path / "site" / "tiles" / "2026092700" / "N00E000.icl").read_bytes())
    assert sq["cube"][0, 0, 0, 0] == 42 and sq["cube"][6, 0, 0, 0] == 0


def test_no_complete_run_is_nothing_to_do(tmp_path):
    deps, _ = _fake_deps(tmp_path)
    deps = deps._replace(listing=lambda hh, field: "")
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=None, squares=None)
    assert rc == run_build.NOTHING_TO_DO


def test_a_failed_file_fails_the_build_not_the_site(tmp_path):
    deps, _ = _fake_deps(tmp_path)
    def bad_download(url, dst):
        raise OSError("boom")
    deps = deps._replace(download=bad_download)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=[0], squares=["N00E000"])
    assert rc == run_build.FAILED
    assert not (tmp_path / "site" / "manifest.json").exists()


class _Resp:
    """A urlopen() result: headers plus a body served in one piece."""
    def __init__(self, body, length):
        self.headers = {"Content-Length": str(length)}
        self._body = body
    def read(self, n):
        b, self._body = self._body[:n], self._body[n:]
        return b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_download_retries_a_short_body_then_raises(tmp_path):
    bodies = [b"12345", b"12345", b"1234567890"]          # short, short, whole
    opens = []
    def urlopen(url, timeout):
        opens.append(url)
        return _Resp(bodies[len(opens) - 1], 10)
    run_build._download("u", tmp_path / "f", urlopen=urlopen)
    assert len(opens) == 3 and (tmp_path / "f").read_bytes() == b"1234567890"
    opens.clear()
    import pytest
    with pytest.raises(OSError, match="truncated"):
        run_build._download("u", tmp_path / "g", urlopen=lambda url, timeout: _Resp(b"12", 10))


def test_a_full_build_publishes_every_square_and_reads_no_land_mask(tmp_path):
    # Erik 2026-10-01: DWD ICON covers the whole globe, so an island or a pin
    # at sea must work the same — no land filter.
    deps, calls = _fake_deps(tmp_path)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=[0], squares=None)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["squares"] == sorted(NINE)
    assert not any("FR_LAND" in u for u in calls["downloads"])


def test_site_squares_is_the_whole_globe(monkeypatch):
    from pipeline.squares import all_squares
    monkeypatch.undo()                      # the real one, not the nine above
    assert run_build.site_squares() == all_squares()


def test_values_are_published_in_2_percent_steps(tmp_path):
    deps, _ = _fake_deps(tmp_path, world_value=lambda step, field: 13)
    run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                    deps=deps, pages_base=None, steps=[0], squares=None)
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    sq = decode_square((tmp_path / "site" / m["tiles"] / "N40W125.icl").read_bytes())
    assert int(sq["cube"].max()) == 14 and int(sq["cube"].min()) == 14


def test_over_the_size_budget_the_squares_are_rewritten_in_4_percent_steps(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_build, "SITE_BUDGET_BYTES", 1)
    deps, _ = _fake_deps(tmp_path, world_value=lambda step, field: 13)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=[0], squares=None)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    sq = decode_square((tmp_path / "site" / m["tiles"] / "N40W125.icl").read_bytes())
    assert int(sq["cube"].max()) == 12
    assert "::warning::" in capsys.readouterr().out


def test_a_partial_live_manifest_does_not_stop_the_full_build(tmp_path):
    deps, calls = _fake_deps(tmp_path, prev_manifest={"run": "2026-09-27T00Z", "complete": False})
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=[0], squares=["N00E000"])
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["complete"] is False                      # a test build never claims to be whole


def test_a_full_build_marks_the_manifest_complete(tmp_path):
    deps, _ = _fake_deps(tmp_path)
    run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                    deps=deps, pages_base=None, steps=None, squares=None)
    assert json.loads((tmp_path / "site" / "manifest.json").read_text())["complete"] is True


def _fake_deps_with_tail(tmp_path, tail_fails=False):
    deps, calls = _fake_deps(tmp_path, world_value=lambda step, field: 90 if step >= 123 else 10)

    def listing(hh, field):
        if hh != "00":
            return ""
        return _listing(RUN, field, steps=list(STEPS) + list(TAIL_STEPS))

    def download(url, dst):
        if tail_fails and "_123_" in url:
            raise OSError("tail down")
        calls["downloads"].append(url)
        Path(dst).write_bytes(b"fake")
    return deps._replace(listing=listing, download=download), calls


def test_a_full_build_publishes_the_tail(tmp_path):
    deps, calls = _fake_deps_with_tail(tmp_path)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=None, squares=None, workers=2)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    base = int(RUN.timestamp()) // 3600
    assert m["tail"]["run"] == "2026-09-27T00Z"
    assert m["tail"]["tiles"] == "tail/2026092700"
    assert m["tail"]["stepsMs"] == [(base + s) * 3600 * 1000 for s in TAIL_STEPS]
    sq = decode_square((tmp_path / "site" / "tail" / "2026092700" / "N40W125.icl").read_bytes())
    assert sq["step_hours"] == [base + s for s in TAIL_STEPS]
    assert int(sq["cube"][0, 0, 0, 0]) == 90
    assert sum(1 for u in calls["downloads"] if "_123_" in u) == len(FIELDS)


def test_a_tail_failure_never_fails_the_build(tmp_path):
    deps, _ = _fake_deps_with_tail(tmp_path, tail_fails=True)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=None, squares=None, workers=2)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["tail"] is None
    assert (tmp_path / "site" / "tiles" / "2026092700" / "N40W125.icl").exists()


def test_a_test_build_makes_no_tail(tmp_path):
    deps, calls = _fake_deps_with_tail(tmp_path)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base="https://x", steps=[0, 1], squares=["N40W125"], workers=2)
    assert rc == run_build.BUILT
    assert json.loads((tmp_path / "site" / "manifest.json").read_text())["tail"] is None
    assert not any("_123_" in u for u in calls["downloads"])


# ---- The column feed --------------------------------------------------------
import re
from pipeline import columns
from pipeline.columns import decode_columns, dequantize, STATIC
from pipeline.dwd import COLUMN_PLEVELS, COLUMN_MLEVELS, column_files

# Fake DWD values in DWD's units, chosen so every app-unit number below is
# computed from them, never typed in.
HSURF_M = 1117.0
# Half levels above ground, as DWD's measured: full 117 → 165 m, 118 → 95.5 m, 119 → 42 m.
HHL_AGL = {117: 200.0, 118: 130.0, 119: 61.0, 120: 23.0}


# Half a quantisation step (columns._QUANT), plus the float32 the remapped
# fields arrive in (~3e-5 K at 300 K).
TOL_T = columns._QUANT[columns.T][0] / 2 + 1e-4
TOL_1 = 0.5 + 1e-3


def _t_kelvin(level, step):
    return 280.0 + 0.5 * step - 0.004 * (1000 - level)


def _column_value(name):
    """The fake field a DWD column file holds, by its name."""
    m = re.search(r"_pressure-level_\d{10}_(\d{3})_(\d+)_([A-Z]+)\.", name)
    if m:
        step, lv, f = int(m.group(1)), int(m.group(2)), m.group(3)
        return lv, {"T": _t_kelvin(lv, step), "RELHUM": 50.0 + lv / 100, "FI": 9.80665 * (8000 - 7 * lv)}[f]
    m = re.search(r"_model-level_\d{10}_(\d{3})_(\d+)_T\.", name)
    if m:
        step, lv = int(m.group(1)), int(m.group(2))
        return lv, 275.0 + step + (118 - lv) * 1.5
    m = re.search(r"_time-invariant_\d{10}_(\d+)_HHL\.", name)
    if m:
        return int(m.group(1)), HSURF_M + HHL_AGL[int(m.group(1))]
    if "_HSURF." in name:
        return None, HSURF_M
    m = re.search(r"_single-level_\d{10}_(\d{3})_(T_2M|RELHUM_2M)\.", name)
    step = int(m.group(1))
    return 2, {"T_2M": 283.15 + step, "RELHUM_2M": 91.0}[m.group(2)]


def _fake_lattice_levels(paths, kit, work, stride):
    shape = columns.lattice(np.zeros((NY, NX), np.uint8), stride).shape
    out = {}
    for p in paths:
        lv, v = _column_value(Path(p).name)
        out[lv] = np.full(shape, v, dtype=np.float32)
    return out


def _deps_with_columns(tmp_path, steps, column_steps=None):
    deps, calls = _fake_deps(tmp_path)
    files = column_files(RUN, steps if column_steps is None else column_steps)

    run = RUN

    def listing(hh, field):
        # The cloud fields as _fake_deps lists them (under 00); the column
        # fields under their run's own hour, as DWD does.
        if field in dwd_fields():
            return _listing(run, field) if hh == "00" else ""
        if hh != f"{run:%H}":
            return ""
        return "".join(f'<a href="{n}">x</a>\n' for n in files.get(field, []))
    return deps._replace(listing=listing, lattice_levels=_fake_lattice_levels), calls


def dwd_fields():
    return FIELDS


def _columns_build(tmp_path, deps, steps=(0, 1, 2), squares=("N40W125",)):
    return run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                           deps=deps, pages_base="https://x", steps=list(steps), squares=list(squares), workers=2)


def _value(d, kind, lt, lv, r=0, c=0, s=0):
    for f, codes in zip(d["spec"], d["fields"]):
        if f[:3] == (kind, lt, lv):
            return float(dequantize(codes[r, c] if f[1] == STATIC else codes[r, c, s], f[3], f[4]))
    raise KeyError((kind, lt, lv))


def test_a_build_publishes_the_columns_in_app_units(tmp_path):
    steps = [0, 1, 2]
    deps, calls = _deps_with_columns(tmp_path, steps)
    assert _columns_build(tmp_path, deps, steps) == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    col = m["columns"]
    base = hour_of(RUN)
    assert col["format"] == "ICC1" and col["run"] == "2026-09-27T00Z"
    assert col["tiles"] == "columns/2026092700"
    assert col["stepsMs"] == [(base + s) * 3600 * 1000 for s in steps]
    assert col["historySteps"] == 0
    assert col["spacing"] == run_build.COLUMN_STRIDE * 0.125
    assert col["side"] == 40 // run_build.COLUMN_STRIDE + 1
    d = decode_columns((tmp_path / "site" / col["tiles"] / "N40W125.icc").read_bytes())
    assert d["step_hours"] == [base + s for s in steps]
    assert (d["rows"], d["cols"]) == (col["side"], col["side"])
    P, H = columns.PRESSURE_HPA, columns.HEIGHT_AGL_M
    for s in steps:
        for lv in COLUMN_PLEVELS:
            assert _value(d, columns.T, P, lv, s=s) == pytest.approx(_t_kelvin(lv, s) - 273.15, abs=TOL_T)
            assert _value(d, columns.RH, P, lv, s=s) == pytest.approx(50.0 + lv / 100, abs=TOL_1)
            assert _value(d, columns.GH, P, lv, s=s) == pytest.approx(8000 - 7 * lv, abs=TOL_1)
        assert _value(d, columns.T, H, 2, s=s) == pytest.approx(283.15 + s - 273.15, abs=TOL_T)
        assert _value(d, columns.RH, H, 2, s=s) == pytest.approx(91.0, abs=TOL_1)
    assert _value(d, columns.HSURF, STATIC, 0) == pytest.approx(HSURF_M, abs=TOL_1)


def test_t80_is_interpolated_in_height_between_the_two_levels_around_80_m(tmp_path):
    steps = [0, 1]
    deps, _ = _deps_with_columns(tmp_path, steps)
    _columns_build(tmp_path, deps, steps)
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    d = decode_columns((tmp_path / "site" / m["columns"]["tiles"] / "N40W125.icc").read_bytes())
    full = {lv: (HHL_AGL[lv] + HHL_AGL[lv + 1]) / 2 for lv in COLUMN_MLEVELS}
    up = max(lv for lv in COLUMN_MLEVELS if full[lv] >= 80)          # the lowest level at or above 80 m
    lo = up + 1
    assert full[lo] < 80 <= full[up]
    for s in steps:
        t_up = _column_value(f"x_model-level_2026092700_{s:03d}_{up}_T.g")[1]
        t_lo = _column_value(f"x_model-level_2026092700_{s:03d}_{lo}_T.g")[1]
        want = t_lo + (t_up - t_lo) * (80 - full[lo]) / (full[up] - full[lo]) - 273.15
        assert _value(d, columns.T, columns.HEIGHT_AGL_M, 80, s=s) == pytest.approx(want, abs=TOL_T)


def test_t80_weights_pick_the_bracketing_pair_per_node_and_never_extrapolate():
    # Four nodes: 80 m between 118 and 119; between 117 and 118 (118 low over
    # steep ground); above all three (clamped to 117); a missing height.
    levels = sorted(COLUMN_MLEVELS)
    full = np.array([[165.0, 95.5, 42.0], [120.0, 70.0, 35.0], [78.0, 60.0, 30.0], [np.nan, 90.0, 40.0]])
    hsurf = np.array([500.0, 0.0, 2000.0, 10.0])
    # Half levels whose midpoints are `full` (the lowest half level on the ground).
    half = {levels[-1] + 1: hsurf.copy()}
    for i, lv in reversed(list(enumerate(levels))):
        half[lv] = 2 * (full[:, i] + hsurf) - half[lv + 1]
    w = run_build.t80_weights(half, hsurf)
    assert sorted(w) == levels
    for node in range(3):
        t = {lv: 280.0 - 0.01 * full[node, i] for i, lv in enumerate(levels)}       # linear in height
        assert sum(w[lv][node] for lv in levels) == pytest.approx(1.0)
        assert all(0.0 <= w[lv][node] <= 1.0 for lv in levels)
        got = sum(w[lv][node] * t[lv] for lv in levels)
        z = full[node]
        want = 280.0 - 0.01 * (80.0 if z.min() <= 80 <= z.max() else z.min() if 80 < z.min() else z.max())
        assert got == pytest.approx(want)
    assert w[levels[0]][2] == 1.0                                    # clamped to the top level
    assert all(np.isnan(w[lv][3]) for lv in levels)


def test_a_full_build_publishes_columns_for_every_square(tmp_path):
    deps, _ = _deps_with_columns(tmp_path, STEPS, column_steps=STEPS)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=None, squares=None, workers=4)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["complete"] is True and m["columns"] is not None
    got = sorted(p.stem for p in (tmp_path / "site" / m["columns"]["tiles"]).glob("*.icc"))
    assert got == sorted(NINE) == m["squares"]
    assert len(m["columns"]["stepsMs"]) == len(STEPS)


def test_column_files_not_yet_listed_wait_then_publish_without_columns(tmp_path, capsys):
    steps = [0, 1]
    deps, calls = _deps_with_columns(tmp_path, steps, column_steps=[0])         # step 1 never lands
    assert _columns_build(tmp_path, deps, steps) == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["columns"] is None
    assert sum(calls["sleeps"]) >= run_build.COLUMN_WAIT_S
    assert (tmp_path / "site" / m["tiles"] / "N40W125.icl").exists()
    assert not (tmp_path / "site" / "columns").exists()
    assert "::warning::columns" in capsys.readouterr().out


def test_a_column_failure_never_fails_the_build(tmp_path, capsys):
    steps = [0, 1]
    deps, _ = _deps_with_columns(tmp_path, steps)

    def broken(paths, kit, work, stride):
        if any("RELHUM_2M" in Path(p).name for p in paths):
            raise OSError("cdo died")
        return _fake_lattice_levels(paths, kit, work, stride)
    assert _columns_build(tmp_path, deps._replace(lattice_levels=broken), steps) == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert m["columns"] is None
    assert not (tmp_path / "site" / "columns").exists()
    assert "::warning::columns failed" in capsys.readouterr().out


def test_column_history_comes_from_state(tmp_path):
    steps = list(range(6))
    deps, _ = _deps_with_columns(tmp_path, steps)
    _columns_build(tmp_path, deps, steps)
    assert (tmp_path / "state" / "columns" / "2026092700.npz").exists()
    # The next run, six hours on, reads those six hours as history.
    global RUN
    first, RUN = RUN, RUN + dt.timedelta(hours=6)
    try:
        deps, _ = _deps_with_columns(tmp_path, [0, 1])
        site2 = tmp_path / "site2"
        run_build.build(site=site2, state=tmp_path / "state", work=tmp_path / "work", deps=deps,
                        pages_base="https://x", steps=[0, 1], squares=["N40W125"], workers=2)
        m = json.loads((site2 / "manifest.json").read_text())
        assert m["columns"]["historySteps"] == 6
        assert m["columns"]["stepsMs"][:6] == [(hour_of(first) + s) * 3600 * 1000 for s in range(6)]
        d = decode_columns((site2 / m["columns"]["tiles"] / "N40W125.icc").read_bytes())
        # History hour s is the first run's step s: T_2M = 283.15 + s K.
        for s in range(6):
            assert _value(d, columns.T, columns.HEIGHT_AGL_M, 2, s=s) == pytest.approx(283.15 + s - 273.15, abs=TOL_T)
    finally:
        RUN = first


def test_the_budget_counts_the_columns_and_drops_them_last(tmp_path, monkeypatch, capsys):
    steps = [0, 1]
    deps, _ = _deps_with_columns(tmp_path, steps)
    _columns_build(tmp_path / "a", deps, steps)
    a = tmp_path / "a" / "site"
    clouds = run_build._dir_bytes(a / "tiles")
    cols = run_build._dir_bytes(a / "columns")
    assert cols > 0
    # A budget the clouds fit but clouds + columns do not: clouds go to 4 %
    # steps first; if that is not enough, the columns are left out.
    monkeypatch.setattr(run_build, "SITE_BUDGET_BYTES", clouds + cols - 1)
    _columns_build(tmp_path / "b", deps, steps)
    b = tmp_path / "b" / "site"
    out = capsys.readouterr().out
    m = json.loads((b / "manifest.json").read_text())
    assert f"{run_build.QUANT_FALLBACK_STEP} % steps" in out
    if run_build._dir_bytes(b / "tiles") + cols > clouds + cols - 1:
        assert m["columns"] is None and not (b / "columns").exists()
    else:
        assert m["columns"] is not None
    # A budget nothing fits: the columns go, the clouds stay.
    monkeypatch.setattr(run_build, "SITE_BUDGET_BYTES", 1)
    _columns_build(tmp_path / "c", deps, steps)
    m = json.loads((tmp_path / "c" / "site" / "manifest.json").read_text())
    assert m["columns"] is None and (tmp_path / "c" / "site" / m["tiles"] / "N40W125.icl").exists()
    assert "leaving the columns out" in capsys.readouterr().out
