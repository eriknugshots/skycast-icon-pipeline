import json
import zlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import aqm.build as ab
import aqm_build
from aqm.build import box_mean, decode_pm, encode_pm, hour_plan, newest_complete_run
from aqm.grib import file_url
from hrrr.grid import SQUARES, bin_index
from pipeline.squares import SIDE, square_name

UTC = timezone.utc


def test_encode_pm():
    assert [encode_pm(p) for p in (0, 9, 35.4, 500, 1000)] == [0, 90, 140, 243, 254]


def test_encode_pm_nan_is_no_data():
    assert encode_pm(float("nan")) == 255


def test_decode_round_trip():
    assert decode_pm(90) == pytest.approx(9.0)


def test_file_url():
    assert file_url(datetime(2026, 11, 7, 12, tzinfo=UTC)) == (
        "https://noaa-nws-naqfc-pds.s3.amazonaws.com/AQMv7/CS/20261107/12/"
        "aqm.t12z.ave_1hr_pm25_bc.20261107.227.grib2")


def test_cycles_are_06_and_12():
    assert newest_complete_run(lambda r: True, datetime(2026, 11, 8, 20, tzinfo=UTC)) == datetime(2026, 11, 8, 12, tzinfo=UTC)
    assert newest_complete_run(lambda r: True, datetime(2026, 11, 8, 3, tzinfo=UTC)) == datetime(2026, 11, 7, 12, tzinfo=UTC)


def test_newest_complete_run_skips_unpublished():
    now = datetime(2026, 11, 8, 20, tzinfo=UTC)
    assert newest_complete_run(lambda r: r.hour == 6, now) == datetime(2026, 11, 8, 6, tzinfo=UTC)
    assert newest_complete_run(lambda r: False, now) is None


def test_hour_plan_uses_newest_run_for_each_hour():
    plan = hour_plan(datetime(2026, 11, 8, 12, tzinfo=UTC))
    assert len(plan) == 96
    assert plan[0] == (datetime(2026, 11, 7, 13, tzinfo=UTC), datetime(2026, 11, 7, 12, tzinfo=UTC), 1)
    assert plan[18] == (datetime(2026, 11, 8, 7, tzinfo=UTC), datetime(2026, 11, 8, 6, tzinfo=UTC), 1)
    assert plan[-1] == (datetime(2026, 11, 11, 12, tzinfo=UTC), datetime(2026, 11, 8, 12, tzinfo=UTC), 72)
    assert all(b[0] - a[0] == timedelta(hours=1) for a, b in zip(plan, plan[1:]))


# A small grid over Bend: 0.05 degree cells, so several voters per 0.125 node.
LAT, LON = np.meshgrid(np.arange(43.0, 46.0, 0.05), np.arange(-123.0, -120.0, 0.05), indexing="ij")
LAT, LON = LAT.ravel().astype(np.float32), LON.ravel().astype(np.float32)
RUN = datetime(2026, 11, 8, 12, tzinfo=UTC)


def fake_read(present, value=lambda cycle, fh: 10.0 + fh, drop=()):
    calls = []

    def read(cycle):
        calls.append(cycle)
        if cycle not in present:
            return None
        hours = {fh: np.full(LAT.size, value(cycle, fh), np.float32) for fh in range(1, 73)
                 if (cycle, fh) not in drop}
        return {"lat": LAT, "lon": LON, "hours": hours}

    read.calls = calls
    return read


def all_cycles():
    return set(ab.cycles_up_to(RUN, 8))


def test_build_writes_squares_and_manifest(tmp_path):
    names = ab.build(RUN, tmp_path, fake_read(all_cycles()), RUN)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["run"] == "2026-11-08T12Z" and m["complete"] is True
    assert m["deg"] == 0.125 and m["square"] == 5 and m["side"] == 41
    assert m["encoding"] == {"scale": 90} and m["attribution"] == "Air quality: NOAA AQM"
    assert m["tiles"] == "tiles/2026110812"
    assert m["squares"] == sorted(names) and len(names) == len(SQUARES)
    assert len(m["stepsMs"]) == 96
    assert all(b - a == 3600000 for a, b in zip(m["stepsMs"], m["stepsMs"][1:]))
    assert m["stepsMs"][-1] == int((RUN + timedelta(hours=72)).timestamp() * 1000)
    tile = zlib.decompress((tmp_path / m["tiles"] / "N40W125.bin").read_bytes())
    cube = np.frombuffer(tile, np.uint8).reshape(96, SIDE, SIDE)
    # Bend (44.06, -121.31) sits in square N40W125.
    r, c = round((44.06 - 40) / 0.125), round((-121.31 + 125) / 0.125)
    # step 0 = 11-07T13Z, forecast hour 1 of the 11-07 12Z cycle: 11 ug/m3.
    assert decode_pm(int(cube[0, r, c])) == pytest.approx(11.0, abs=0.15)
    assert decode_pm(int(cube[-1, r, c])) == pytest.approx(82.0, rel=0.03)
    # Far from the fake grid: no data.
    assert int(cube[0, 0, 0]) == 255


def test_manifest_written_last_and_names_every_square(tmp_path, monkeypatch):
    order = []
    real = zlib.compress

    def spy(b, level=-1):
        order.append("tile")
        return real(b, level)

    monkeypatch.setattr(ab.zlib, "compress", spy)
    orig = type(tmp_path).write_text

    def write_text(self, *a, **k):
        order.append(self.name)
        return orig(self, *a, **k)

    monkeypatch.setattr(type(tmp_path), "write_text", write_text)
    names = ab.build(RUN, tmp_path, fake_read(all_cycles()), RUN)
    assert order[-1] == "manifest.json" and order.count("tile") == len(SQUARES)
    assert len(json.loads((tmp_path / "manifest.json").read_text())["squares"]) == len(names)


def test_missing_earlier_run_starts_later_and_still_builds(tmp_path):
    ab.build(RUN, tmp_path, fake_read({RUN}), RUN)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert len(m["stepsMs"]) == 72
    assert m["stepsMs"][0] == int((RUN + timedelta(hours=1)).timestamp() * 1000)
    assert all(b - a == 3600000 for a, b in zip(m["stepsMs"], m["stepsMs"][1:]))


def test_missing_middle_cycle_falls_back_to_the_older_one(tmp_path):
    present = all_cycles() - {datetime(2026, 11, 8, 6, tzinfo=UTC)}
    ab.build(RUN, tmp_path, fake_read(present), RUN)
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert len(m["stepsMs"]) == 96     # 11-07 12Z fh 19..24 cover 07Z..12Z


def test_a_missing_hour_of_the_newest_run_fails_the_build(tmp_path):
    with pytest.raises(RuntimeError, match="lacks forecast hours"):
        ab.build(RUN, tmp_path, fake_read(all_cycles(), drop={(RUN, 40)}), RUN)
    assert not (tmp_path / "manifest.json").exists()


def test_unpublished_run_fails_the_build(tmp_path):
    with pytest.raises(RuntimeError):
        ab.build(RUN, tmp_path, fake_read(set()), RUN)


def test_a_failing_download_is_retried(tmp_path):
    inner = fake_read(all_cycles())
    n = {"fails": 2}

    def flaky(cycle):
        if cycle == RUN and n["fails"]:
            n["fails"] -= 1
            raise OSError("reset")
        return inner(cycle)

    ab.build(RUN, tmp_path, flaky, RUN, sleep=lambda s: None)
    assert (tmp_path / "manifest.json").exists()


def test_box_mean_skips_nan_and_marks_empty_nodes_255():
    lat = np.array([40.0, 40.0, 40.0, 40.0], np.float32)
    lon = np.array([-125.0, -125.0, -125.0, -124.875], np.float32)
    bins = bin_index(lat, lon)
    out = box_mean(np.array([10.0, 30.0, np.nan, 150.0], np.float32), bins)["N40W125"]
    assert decode_pm(int(out[0, 0])) == pytest.approx(20.0, abs=0.3)      # mean of 10 and 30
    assert decode_pm(int(out[0, 1])) == pytest.approx(150.0, rel=0.02)    # not clipped at 100
    assert int(out[5, 5]) == 255
    nan_only = box_mean(np.array([np.nan] * 4, np.float32), bins)["N40W125"]
    assert int(nan_only[0, 0]) == 255


def test_squares_past_the_domain_are_all_255(tmp_path):
    ab.build(RUN, tmp_path, fake_read(all_cycles()), RUN)
    m = json.loads((tmp_path / "manifest.json").read_text())
    cube = zlib.decompress((tmp_path / m["tiles"] / f"{square_name(15, -140)}.bin").read_bytes())
    assert set(cube) == {255}


def test_main_exit_codes(tmp_path):
    class Everything(set):
        def __contains__(self, item):
            return True

    ok = fake_read(Everything())
    assert aqm_build.main(["--out", str(tmp_path / "a")], read=ok, exists=lambda r: True) == 0
    assert aqm_build.main(["--out", str(tmp_path / "b")], read=ok, exists=lambda r: False) == 1
    assert aqm_build.main(["--out", str(tmp_path / "c")], read=lambda c: None, exists=lambda r: True) == 1
    assert not (tmp_path / "c" / "manifest.json").exists()


def test_main_returns_3_when_the_live_manifest_already_names_the_run(tmp_path, monkeypatch):
    class Everything(set):
        def __contains__(self, item):
            return True

    iso = aqm_build.run_iso(ab.newest_complete_run(lambda r: True, aqm_build._now()))
    monkeypatch.setattr(aqm_build, "_http_text", lambda url: json.dumps({"run": iso}))
    out = tmp_path / "out"
    rc = aqm_build.main(["--out", str(out), "--live-manifest", "https://x/manifest.json"],
                        read=fake_read(Everything()), exists=lambda r: True)
    assert rc == 3
    assert not out.exists()           # nothing built
    monkeypatch.setattr(aqm_build, "_http_text", lambda url: json.dumps({"run": "1999-01-01T00:00:00Z"}))
    assert aqm_build.main(["--out", str(out), "--live-manifest", "https://x/manifest.json"],
                          read=fake_read(Everything()), exists=lambda r: True) == 0
