import json
from datetime import datetime, timezone

import pytest

import hrrr.build as hb
from hrrr.grib import FIELDS, file_url
from hrrr.grid import SQUARES
from pipeline.encode import decode_square
from pipeline.squares import square_name
from tests.test_hrrr_grib import _msg

UTC = timezone.utc


def test_last_hour():
    for h in range(24):
        run = datetime(2026, 11, 6, h, tzinfo=UTC)
        assert hb.last_hour(run) == (48 if h % 6 == 0 else 18)


def test_newest_complete_run_skips_incomplete():
    now = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
    present = {file_url(datetime(2026, 11, 6, 14, tzinfo=UTC), 18) + ".idx"}
    asked = []

    def exists(url):
        asked.append(url)
        return url in present

    assert hb.newest_complete_run(exists, now) == datetime(2026, 11, 6, 14, tzinfo=UTC)
    assert asked[0] == file_url(datetime(2026, 11, 6, 15, tzinfo=UTC), 18) + ".idx"


def test_newest_complete_run_none():
    now = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
    assert hb.newest_complete_run(lambda u: False, now) is None


def _canned_get():
    msgs = {
        "LCDC": _msg([10, 20, 30, 40]),
        "MCDC": _msg([11, 21, 31, 41]),
        "HCDC": _msg([12, 22, 32, 42]),
        "TCDC": _msg([13, 23, 33, 43]),
    }
    pos, lines, ranges = 0, [], {}
    for i, (name, level) in enumerate(FIELDS, 1):
        n = len(msgs[name])
        lines.append(f"{i}:{pos}:d=2026110612:{name}:{level}:5 hour fcst:")
        ranges[(pos, pos + n - 1 if name != "TCDC" else None)] = name
        pos += n
    idx = ("\n".join(lines) + "\n").encode()

    def get(url, rng=None):
        if url.endswith(".idx"):
            return idx
        return msgs[ranges[rng]]

    return get


def test_build_writes_every_square_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "last_hour", lambda run: 2)
    run = datetime(2026, 11, 6, 12, tzinfo=UTC)
    now = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
    names = hb.build(run, tmp_path, _canned_get(), now)
    want = [square_name(a, b) for a, b in SQUARES]
    assert names == want
    for n in want:
        assert (tmp_path / "tiles" / "2026110612" / f"{n}.icl").is_file()
    m = json.loads((tmp_path / "manifest.json").read_text())
    base = int(run.timestamp() // 3600)
    assert m["stepsMs"] == [(base + k) * 3_600_000 for k in range(3)]
    assert m["model"] == "hrrr"
    assert m["attribution"] == "Cloud data: NOAA HRRR"
    assert m["run"] == "2026-11-06T12Z"
    assert m["historySteps"] == 0
    for k in ("thick", "blocks", "columns", "tail"):
        assert k not in m
    assert m["squares"] == sorted(want)
    far = decode_square((tmp_path / "tiles/2026110612" / f"{square_name(15, -140)}.icl").read_bytes())
    assert (far["cube"] == 255).all()
    assert far["step_hours"] == [base + k for k in range(3)]
    near = decode_square((tmp_path / "tiles/2026110612" / f"{square_name(40, -110)}.icl").read_bytes())
    assert near["cube"].shape == (3, 4, 41, 41)
    assert (near["cube"] != 255).any()
    # the four fields land in low/mid/high/total order
    vals = sorted({int(v) for v in near["cube"][0, 0].ravel() if v != 255})
    assert vals and all(10 <= v <= 40 for v in vals)


def test_build_fetch_failure_retries_then_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "last_hour", lambda run: 0)
    calls = []

    def get(url, rng=None):
        calls.append(url)
        raise OSError("boom")

    with pytest.raises(OSError):
        hb.build(datetime(2026, 11, 6, 12, tzinfo=UTC), tmp_path, get, datetime(2026, 11, 6, 15, tzinfo=UTC), sleep=lambda s: None)
    assert len(calls) == 3


def test_cli_nothing_to_do(tmp_path, monkeypatch):
    import hrrr_build
    now = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
    run = datetime(2026, 11, 6, 14, tzinfo=UTC)
    monkeypatch.setattr(hrrr_build, "_now", lambda: now)
    monkeypatch.setattr(hrrr_build, "_exists", lambda u: u.startswith(file_url(run, 18)))
    monkeypatch.setattr(hrrr_build, "_http_text", lambda u: json.dumps({"run": "2026-11-06T14Z"}))
    assert hrrr_build.main(["--out", str(tmp_path), "--live-manifest", "http://x/manifest.json"]) == 3
    assert not (tmp_path / "manifest.json").exists()


def test_cli_builds_when_live_missing(tmp_path, monkeypatch):
    import hrrr_build
    monkeypatch.setattr(hb, "last_hour", lambda run: 0)
    now = datetime(2026, 11, 6, 15, 40, tzinfo=UTC)
    monkeypatch.setattr(hrrr_build, "_now", lambda: now)
    monkeypatch.setattr(hrrr_build, "_exists", lambda u: u.startswith(file_url(datetime(2026, 11, 6, 14, tzinfo=UTC), 18)) or True)
    def boom(u):
        raise OSError("404")
    monkeypatch.setattr(hrrr_build, "_http_text", boom)
    monkeypatch.setattr(hrrr_build, "_get", _canned_get())
    assert hrrr_build.main(["--out", str(tmp_path), "--live-manifest", "http://x/manifest.json"]) == 0
    assert (tmp_path / "manifest.json").exists()


def test_cli_failed_build_exits_1(tmp_path, monkeypatch):
    import hrrr_build
    monkeypatch.setattr(hb, "last_hour", lambda run: 0)
    monkeypatch.setattr(hrrr_build, "_now", lambda: datetime(2026, 11, 6, 15, 40, tzinfo=UTC))
    monkeypatch.setattr(hrrr_build, "_exists", lambda u: True)
    def bad(u, rng=None):
        raise OSError("x")
    monkeypatch.setattr(hrrr_build, "_get", bad)
    monkeypatch.setattr(hrrr_build, "_sleep", lambda s: None)
    assert hrrr_build.main(["--out", str(tmp_path)]) == 1
