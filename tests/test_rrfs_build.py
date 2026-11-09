import json
from datetime import datetime, timezone

import numpy as np
import pytest

import rrfs.build as rb
from pipeline.encode import decode_square
from pipeline.site import tiles_path
from pipeline.squares import SIDE, square_name
from rrfs.grib import FIELDS, file_url
from rrfs.grid import SQUARES
from tests.gribfake import Files, msg

UTC = timezone.utc
RUN = datetime(2026, 11, 9, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 9, 16, 12, tzinfo=UTC)


def test_step_list_is_hourly_to_84():
    assert rb.STEPS == tuple(range(0, 85)) and rb.LAST_HOUR == 84
    assert rb.RUN_HOURS == (0, 6, 12, 18)


def test_newest_complete_run_scans_four_cycles_by_the_f084_idx():
    # 16:12Z: the newest cycle is 12Z; its f084 is not there, 06Z's is.
    present = {file_url(datetime(2026, 11, 9, 6, tzinfo=UTC), 84) + ".idx"}
    asked = []

    def exists(u):
        asked.append(u)
        return u in present

    assert rb.newest_complete_run(exists, NOW) == datetime(2026, 11, 9, 6, tzinfo=UTC)
    assert asked == [file_url(datetime(2026, 11, 9, 12, tzinfo=UTC), 84) + ".idx",
                     file_url(datetime(2026, 11, 9, 6, tzinfo=UTC), 84) + ".idx"]


def test_newest_complete_run_looks_back_exactly_four_cycles():
    asked = []
    assert rb.newest_complete_run(lambda u: asked.append(u) or False, NOW) is None
    assert [u.split("/")[-3:-1] for u in asked] == [["rrfs.20261109", "12"], ["rrfs.20261109", "06"],
                                                    ["rrfs.20261109", "00"], ["rrfs.20261108", "18"]]


def _files(hours):
    f = Files()
    for fh in hours:
        text = "anl" if fh == 0 else f"{fh} hour fcst"
        rows = [("PRES", "surface", text, msg([1, 2, 3, 4]))]
        for k, (n, lv) in enumerate(FIELDS):
            rows.append((n, lv, f"{max(fh - 1, 0)}-{fh} hour ave fcst", msg([99] * 4)))
            rows.append((n, lv, text, msg([5 * j * (k + 1) + fh for j in (1, 2, 3, 4)])))
        f.add(file_url(RUN, fh), rows)
    return f


def test_build_writes_every_square_and_the_manifest(tmp_path):
    f = _files([0, 1, 2])
    names = rb.build(RUN, tmp_path, f.get, NOW, sleep=lambda s: None, fhs=[0, 1, 2])
    want = [square_name(a, b) for a, b in SQUARES]
    assert names == want and len(names) == 416
    for n in want:
        assert (tmp_path / tiles_path(RUN) / f"{n}.icl").is_file()
    m = json.loads((tmp_path / "manifest.json").read_text())
    base = int(RUN.timestamp() // 3600)
    assert m["model"] == "rrfs" and m["attribution"] == "Cloud data: NOAA RRFS"
    assert m["stepsMs"] == [(base + k) * 3_600_000 for k in range(3)]
    assert m["run"] == "2026-11-09T12Z" and m["historySteps"] == 0
    assert m["tiles"] == tiles_path(RUN) and m["squares"] == sorted(want)
    for k in ("blocks", "thick", "columns", "tail"):
        assert k not in m
    assert m["complete"] is False             # a sample of three hours says so


def test_build_node_values_fill_and_far_squares(tmp_path):
    f = _files([0, 1])
    rb.build(RUN, tmp_path, f.get, NOW, sleep=lambda s: None, fhs=[0, 1])
    d = decode_square((tmp_path / tiles_path(RUN) / "N40W110.icl").read_bytes())
    c = d["cube"]
    assert c.shape == (2, 4, SIDE, SIDE)
    # f001: LCDC = 5 * j + 1 = 6, 11, 16, 21 at (41,-110) (41,-109) (40,-110) (40,-109); rows 0 and 8, columns 0 and 8
    assert (c[1, 0, 8, 0], c[1, 0, 8, 8], c[1, 0, 0, 0], c[1, 0, 0, 8]) == (6, 11, 16, 21)
    assert c[1, 0, 0, 1] == 16          # empty node next to one voter takes its value
    assert c[1, 0, 4, 4] == 255         # four nodes from every voter
    # the four fields land in low / mid / high / total order
    # the third cell is 15 * (k + 1) + 1: low 16, mid 31, high 46, total 61
    assert [int(c[1, k, 0, 0]) for k in range(4)] == [16, 31, 46, 61]
    far = decode_square((tmp_path / tiles_path(RUN) / f"{square_name(*SQUARES[0])}.icl").read_bytes())
    assert (far["cube"] == 255).all()


def test_complete_manifest_for_the_whole_step_list(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "STEPS", (0, 1))
    monkeypatch.setattr(rb, "LAST_HOUR", 1)
    f = _files([0, 1])
    rb.build(RUN, tmp_path, f.get, NOW, sleep=lambda s: None)
    assert json.loads((tmp_path / "manifest.json").read_text())["complete"] is True


def test_fetch_failure_retries_then_raises(tmp_path):
    calls = []

    def get(url, rng=None):
        calls.append(url)
        raise OSError("boom")

    with pytest.raises(OSError):
        rb.build(RUN, tmp_path, get, NOW, sleep=lambda s: None, fhs=[0])
    assert len(calls) == 3


def test_default_workers_is_two(monkeypatch):
    monkeypatch.delenv("RRFS_WORKERS", raising=False)
    assert rb.workers() == 2
    monkeypatch.setenv("RRFS_WORKERS", "1")
    assert rb.workers() == 1


# ---- the command line ------------------------------------------------------

def test_cli_nothing_to_do(tmp_path, monkeypatch):
    import rrfs_build
    monkeypatch.setattr(rrfs_build, "_now", lambda: NOW)
    monkeypatch.setattr(rrfs_build, "_exists", lambda u: u.startswith(file_url(RUN, 84)))
    monkeypatch.setattr(rrfs_build, "_http_text", lambda u: json.dumps({"run": "2026-11-09T12Z"}))
    assert rrfs_build.main(["--out", str(tmp_path), "--live-manifest", "http://x/manifest.json"]) == 3
    assert not (tmp_path / "manifest.json").exists()


def test_cli_builds_a_sample_and_rate_limits_every_request(tmp_path, monkeypatch):
    import rrfs_build
    f = _files([0, 3])
    n = {"acquired": 0}

    class Counting:
        def acquire(self):
            n["acquired"] += 1

    monkeypatch.setattr(rrfs_build, "_now", lambda: NOW)
    monkeypatch.setattr(rrfs_build, "_exists", lambda u: u.startswith(file_url(RUN, 84)))
    monkeypatch.setattr(rrfs_build, "_get_raw", f.get)
    monkeypatch.setattr(rrfs_build, "LIMITER", Counting())
    assert rrfs_build.main(["--out", str(tmp_path), "--hours", "0,3"]) == 0
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert len(m["stepsMs"]) == 2 and m["complete"] is False
    assert n["acquired"] == len(f.calls) == 2 * 5        # one idx and four ranges an hour, all counted


def test_cli_failed_build_exits_1(tmp_path, monkeypatch):
    import rrfs_build
    monkeypatch.setattr(rrfs_build, "_now", lambda: NOW)
    monkeypatch.setattr(rrfs_build, "_exists", lambda u: True)

    def bad(u, rng=None):
        raise OSError("x")

    monkeypatch.setattr(rrfs_build, "_get_raw", bad)
    monkeypatch.setattr(rrfs_build, "_sleep", lambda s: None)
    monkeypatch.setattr(rrfs_build, "LIMITER", rb.RateLimiter(10**6))
    assert rrfs_build.main(["--out", str(tmp_path), "--hours", "0"]) == 1


def test_cli_no_complete_run_exits_1(tmp_path, monkeypatch):
    import rrfs_build
    monkeypatch.setattr(rrfs_build, "_now", lambda: NOW)
    monkeypatch.setattr(rrfs_build, "_exists", lambda u: False)
    assert rrfs_build.main(["--out", str(tmp_path)]) == 1
