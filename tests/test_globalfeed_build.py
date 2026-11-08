import dataclasses
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import globalfeed.build as gb
from globalfeed import cli, history6
from globalfeed.latlon import to_feed
from globalfeed.models import GFS, AIFS
from pipeline import blocks, history
from pipeline.encode import decode_square, quantize
from pipeline.site import tiles_path, tail_path, blocks_path
from pipeline.squares import NX, NY, square_name, slice_square

UTC = timezone.utc
RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 8, 5, 10, tzinfo=UTC)
SQUARES = [square_name(40, -125), square_name(-5, 175), square_name(85, 0)]


def _small(model, main, tail):
    return dataclasses.replace(model, main_steps=tuple(main), tail_steps=tuple(tail))


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(gb, "all_squares", lambda: list(SQUARES))
    monkeypatch.setattr(gb.history, "HEAD_STEPS", 6)

    def fake_read(model, run, fh, get):
        get(model.index_url(run, fh))        # a failing get must fail the step
        lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
        out = {"lat": lat.ravel(), "lon": lon.ravel()}
        for i, f in enumerate(model.fields):
            k = f if isinstance(f, str) else f[0]
            out[k] = ((lat.ravel() + 90) / 1.8 + fh * 2 + i * 7) % 101
        return out

    monkeypatch.setattr(gb, "read_step", fake_read)
    return fake_read


def _get(url, rng=None):
    return b""


def _head(value):
    return np.full((1, 4, NY, NX), value, dtype=np.uint8)


def test_newest_complete_run():
    present = {GFS.index_url(datetime(2026, 11, 7, 18, tzinfo=UTC), GFS.tail_steps[-1])}
    asked = []

    def exists(u):
        asked.append(u)
        return u in present

    assert gb.newest_complete_run(GFS, exists, NOW) == datetime(2026, 11, 7, 18, tzinfo=UTC)
    assert asked[0] == GFS.index_url(RUN, GFS.tail_steps[-1])
    assert gb.newest_complete_run(GFS, lambda u: False, NOW) is None


def test_build_layout_and_manifest(small, tmp_path):
    m = _small(GFS, range(0, 3), (3, 6))
    names = gb.build(m, RUN, tmp_path, _get, NOW, tmp_path / "state", sleep=lambda s: None)
    assert names == SQUARES
    man = json.loads((tmp_path / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["tail"]["stepsMs"] == [(base + s) * 3600 * 1000 for s in m.tail_steps]
    assert man["tail"]["tiles"] == tail_path(RUN) and man["tail"]["run"] == man["run"]
    assert man["model"] == m.name and man["attribution"] == m.attribution
    assert "thick" not in man and "columns" not in man
    assert man["blocks"] == blocks_path(RUN) and man["tiles"] == tiles_path(RUN)
    assert man["historySteps"] == 0
    assert man["stepsMs"] == [(base + s) * 3600 * 1000 for s in m.main_steps]
    # Expected cube for one square, computed through the same public pieces.
    lat, lon = np.meshgrid(np.arange(-90, 90.25, 0.25), np.arange(-180, 180, 0.25), indexing="ij")
    for n in SQUARES:
        sl, so = int(n[1:3]) * (1 if n[0] == "N" else -1), int(n[4:]) * (1 if n[3] == "E" else -1)
        icl = decode_square((tmp_path / tiles_path(RUN) / f"{n}.icl").read_bytes())
        icb = blocks.decode_blocks((tmp_path / blocks_path(RUN) / f"{n}.icb").read_bytes(), 4)
        assert (tmp_path / tail_path(RUN) / f"{n}.icl").exists()
        assert np.array_equal(icl["cube"], icb["cube"])
        assert icl["step_hours"] == icb["step_hours"] == [base + s for s in m.main_steps]
        step = fake_cube(m, 1, lat, lon)
        want = quantize(slice_square(step, sl, so)[None], 2)[0]
        assert np.array_equal(icl["cube"][1], want)


def fake_cube(m, fh, lat, lon):
    return np.stack([to_feed(lat.ravel(), lon.ravel(), ((lat.ravel() + 90) / 1.8 + fh * 2 + i * 7) % 101)
                     for i in range(len(m.fields))])


def test_history_gfs(small, tmp_path):
    m = _small(GFS, range(0, 3), (3, 6))
    state = tmp_path / "state"
    earlier = RUN - timedelta(hours=6)
    history.save_run_head(state, earlier, np.zeros((6, 4, NY, NX), dtype=np.uint8), sub=m.name)
    gb.build(m, RUN, tmp_path / "o", _get, NOW, state, sleep=lambda s: None)
    man = json.loads((tmp_path / "o" / "manifest.json").read_text())
    base = int(earlier.timestamp() // 3600)
    assert man["historySteps"] == history.HEAD_STEPS
    assert man["stepsMs"][:history.HEAD_STEPS] == [(base + i) * 3600 * 1000 for i in range(history.HEAD_STEPS)]
    assert len(man["stepsMs"]) == history.HEAD_STEPS + len(m.main_steps)


def test_history_aifs(small, tmp_path):
    m = _small(AIFS, (0, 6, 12), (18, 24))
    state = tmp_path / "state"
    ages = (18, 12, 6)
    for a in ages:
        history6.save_run_head6(state, RUN - timedelta(hours=a), _head(a))
    history6.save_run_head6(state, RUN - timedelta(hours=66), _head(66))
    gb.build(m, RUN, tmp_path / "o", _get, NOW, state, sleep=lambda s: None)
    man = json.loads((tmp_path / "o" / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert man["historySteps"] == len(ages)
    assert man["stepsMs"][:3] == [(base - a) * 3600 * 1000 for a in ages]
    gaps = np.diff(man["stepsMs"]) // 3600000
    assert set(gaps) == {6}
    assert list(history6.load_history6(state, RUN)) == [base - a for a in ages]
    # The build saved its own f000 for the next run.
    assert base in history6.load_history6(state, RUN + timedelta(hours=6))


def test_history6_keeps_newest_ten(tmp_path):
    for i in range(12):
        history6.save_run_head6(tmp_path, RUN - timedelta(hours=6 * i), _head(i))
    assert len(list((tmp_path / history6.SUB).glob("*.npz"))) == history.KEEP_RUNS


def test_cli_nothing_to_do(tmp_path):
    iso = RUN.strftime("%Y-%m-%dT%HZ")
    rc = cli.main(GFS, ["--out", str(tmp_path), "--state", str(tmp_path), "--live-manifest", "http://x"],
                  now=NOW, exists=lambda u: True, http_text=lambda u: json.dumps({"run": iso}))
    assert rc == 3


def test_cli_failure_is_1(tmp_path):
    def boom(url, rng=None):
        raise OSError("down")

    rc = cli.main(_small(GFS, range(0, 2), (3,)), ["--out", str(tmp_path), "--state", str(tmp_path)],
                  now=NOW, exists=lambda u: True, get=boom, sleep=lambda s: None)
    assert rc == 1
