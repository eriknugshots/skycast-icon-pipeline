import json
from datetime import datetime, timezone

import hrrr_fog_build as fb

UTC = timezone.utc
RUN = "2026-11-10T12Z"
LIVE = {"model": "hrrr", "run": RUN, "hours": [1, 2], "tiles": "tiles/2026111012", "attribution": "x"}
ENTRY = {"format": "ICC1", "run": RUN, "tiles": 3}
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


def test_fog_build_builds_for_live_run(tmp_path):
    calls = []
    assert _run(tmp_path, _fetch(LIVE), _build(calls)) == 0
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
