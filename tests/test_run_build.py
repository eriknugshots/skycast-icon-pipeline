import datetime as dt
import json
import numpy as np
from pathlib import Path
from pipeline.dwd import STEPS, FIELDS, file_name
from pipeline.encode import decode_square
from pipeline.history import save_run_head, hour_of
from pipeline.squares import NX, NY
import run_build

UTC = dt.timezone.utc
RUN = dt.datetime(2026, 9, 27, 0, tzinfo=UTC)


def _listing(run, field, steps=STEPS):
    return "".join(f'<a href="{file_name(run, s, field)}">x</a>\n' for s in steps)


def _fake_deps(tmp_path, world_value=lambda step, field: 0, prev_manifest=None):
    calls = {"downloads": [], "regrids": []}

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
        if name.startswith("FR_LAND"):
            # Land only inside N40W125 (a 0-1 fraction, scaled to percent by the caller).
            frac = np.zeros((NY, NX), dtype=np.uint8)
            frac[1050, 450] = 100
            return frac
        step = int(name.split("_")[-2]); field = name.split("_")[-1].split(".")[0]
        calls["regrids"].append((step, field))
        return np.full((NY, NX), world_value(step, field), dtype=np.uint8)

    return run_build.Deps(listing=listing, fetch_text=fetch_text, download=download,
                          regrid=regrid, ensure_kit=lambda work: Path("/kit"),
                          now=lambda: dt.datetime(2026, 9, 27, 3, tzinfo=UTC)), calls


def test_early_exit_when_the_site_already_has_this_run(tmp_path, capsys):
    deps, calls = _fake_deps(tmp_path, prev_manifest={"run": "2026-09-27T00Z"})
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


def test_a_full_build_keeps_only_land_adjacent_squares(tmp_path):
    deps, calls = _fake_deps(tmp_path)
    rc = run_build.build(site=tmp_path / "site", state=tmp_path / "state", work=tmp_path / "work",
                         deps=deps, pages_base=None, steps=[0], squares=None)
    assert rc == run_build.BUILT
    m = json.loads((tmp_path / "site" / "manifest.json").read_text())
    assert len(m["squares"]) == 9 and "N40W125" in m["squares"] and "S10E100" not in m["squares"]
    assert any(u.endswith("_FR_LAND.grib2.bz2") for u in calls["downloads"])
