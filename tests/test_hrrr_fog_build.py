import json
from datetime import datetime, timezone

import hrrr_fog_build as fb

UTC = timezone.utc
RUN = "2026-11-10T12Z"
LIVE = {"model": "hrrr", "run": RUN, "hours": [1, 2], "tiles": "tiles/2026111012", "attribution": "x"}
ENTRY = {"format": "ICC1", "run": RUN, "tiles": "fog/2026111012", "tileNames": ["a", "b", "c"]}
NOW = datetime(2026, 11, 10, 15, tzinfo=UTC)


def _fetch(*manifests):
    seq = list(manifests)

    def fetch_json(url):
        return json.loads(json.dumps(seq.pop(0) if len(seq) > 1 else seq[0]))
    return fetch_json


def _build(calls=None):
    def build(run, out_dir, get, steps):
        if calls is not None:
            calls.append((run, list(steps)))
        return dict(ENTRY)
    return build


def _run(tmp_path, fetch, build, get=lambda u, r=None: b"", extra=()):
    return fb.main(["--out", str(tmp_path / "o"), "--live-manifest", "http://m", *extra],
                   fetch_json=fetch, get=get, now=lambda: NOW, sleep=lambda s: None, build=build)


def test_fog_build_builds_for_live_run(tmp_path, capsys):
    calls = []
    assert _run(tmp_path, _fetch(LIVE), _build(calls)) == 0
    assert "3 tiles" in capsys.readouterr().out
    m = json.loads((tmp_path / "o" / "manifest.json").read_text())
    assert m["fog"] == ENTRY
    assert {k: v for k, v in m.items() if k != "fog"} == LIVE
    run, steps = calls[0]
    assert run == datetime(2026, 11, 10, 12, tzinfo=UTC) and steps == list(range(49))


def test_fog_build_short_run_has_19_steps(tmp_path):
    calls = []
    live = dict(LIVE, run="2026-11-10T13Z")
    assert _run(tmp_path, _fetch(live), _build(calls)) == 0
    assert calls[0][1] == list(range(19))


def test_fog_build_nothing_to_do(tmp_path):
    live = dict(LIVE, fog={"run": RUN})
    assert _run(tmp_path, _fetch(live), _build()) == 3
    assert not (tmp_path / "o" / "manifest.json").exists()


def test_fog_build_skips_when_live_run_moved(tmp_path):
    moved = dict(LIVE, run="2026-11-10T13Z")
    assert _run(tmp_path, _fetch(LIVE, moved), _build()) == 3
    assert not (tmp_path / "o" / "manifest.json").exists()


def test_fog_build_failure_exit_1(tmp_path):
    def boom(run, out_dir, get, steps):
        raise RuntimeError("no")
    assert _run(tmp_path, _fetch(LIVE), boom) == 1
    assert not (tmp_path / "o" / "manifest.json").exists()


def test_fog_build_no_live_manifest_fails(tmp_path):
    assert fb.main(["--out", str(tmp_path / "o")], fetch_json=_fetch(LIVE), build=_build()) == 1



def _check(tmp_path, live, built_run=RUN):
    d = tmp_path / "built"
    d.mkdir(exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(dict(LIVE, run=built_run, fog=dict(ENTRY, run=built_run))))
    return fb.main(["--still-live", str(d), "--live-manifest", "http://m"],
                   fetch_json=_fetch(live), build=_build())


def test_still_live_same_run(tmp_path):
    assert _check(tmp_path, LIVE) == 0


def test_still_live_run_moved(tmp_path, capsys):
    assert _check(tmp_path, dict(LIVE, run="2026-11-10T13Z")) == 3
    assert "live run is now 2026-11-10T13Z" in capsys.readouterr().out


def test_still_live_missing_manifest_fails(tmp_path):
    assert fb.main(["--still-live", str(tmp_path / "nope"), "--live-manifest", "http://m"],
                   fetch_json=_fetch(LIVE), build=_build()) == 1


def test_still_live_never_builds(tmp_path):
    calls = []
    d = tmp_path / "built"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(dict(LIVE, fog=ENTRY)))
    fb.main(["--still-live", str(d), "--live-manifest", "http://m"], fetch_json=_fetch(LIVE), build=_build(calls))
    assert calls == []

def test_get_retries_once_then_builds(tmp_path):
    n = {"c": 0}

    def get(url, rng=None):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("hiccup")
        return b"ok"

    def build(run, out_dir, g, steps):
        assert g("u") == b"ok"
        return dict(ENTRY)
    assert _run(tmp_path, _fetch(LIVE), build, get=get) == 0
    assert n["c"] == 2


def test_persistent_get_failure_exit_1(tmp_path):
    n = {"c": 0}

    def get(url, rng=None):
        n["c"] += 1
        raise OSError("down")

    def build(run, out_dir, g, steps):
        g("u")
        return dict(ENTRY)
    assert _run(tmp_path, _fetch(LIVE), build, get=get) == 1
    assert n["c"] == fb.RETRIES + 1
    assert not (tmp_path / "o" / "manifest.json").exists()


def test_older_fog_key_rebuilds(tmp_path):
    live = dict(LIVE, fog={"run": "2026-11-10T06Z"})
    assert _run(tmp_path, _fetch(live), _build()) == 0
    assert json.loads((tmp_path / "o" / "manifest.json").read_text())["fog"]["run"] == RUN


class _Resp:
    def __init__(self, body, cl=None):
        self._b, self.headers = body, ({"Content-Length": str(cl)} if cl is not None else {})

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_rejects_short_closed_range():
    import pytest
    with pytest.raises(IOError):
        fb._get("http://u", (0, 9), opener=lambda req, timeout: _Resp(b"12345"))
    assert fb._get("http://u", (0, 4), opener=lambda req, timeout: _Resp(b"12345")) == b"12345"


def test_get_open_range_checks_content_length():
    import pytest
    with pytest.raises(IOError):
        fb._get("http://u", (5, None), opener=lambda req, timeout: _Resp(b"123", cl=10))
    assert fb._get("http://u", (5, None), opener=lambda req, timeout: _Resp(b"123", cl=3)) == b"123"


def test_short_body_once_is_retried_then_builds(tmp_path):
    bodies = [b"12", b"12345"]

    def get(url, rng=None):
        return fb._get(url, rng, opener=lambda req, timeout: _Resp(bodies.pop(0)))

    def build(run, out_dir, g, steps):
        assert g("http://u", (0, 4)) == b"12345"
        return dict(ENTRY)
    assert _run(tmp_path, _fetch(LIVE), build, get=get) == 0
    assert bodies == []


def test_persistent_short_body_exit_1(tmp_path):
    def get(url, rng=None):
        return fb._get(url, rng, opener=lambda req, timeout: _Resp(b"12"))

    def build(run, out_dir, g, steps):
        g("http://u", (0, 4))
        return dict(ENTRY)
    assert _run(tmp_path, _fetch(LIVE), build, get=get) == 1
    assert not (tmp_path / "o" / "manifest.json").exists()
