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
        step = int(name.split("_")[-2]); field = name.split("_")[-1].split(".")[0]
        calls["regrids"].append((step, field))
        return np.full((NY, NX), world_value(step, field), dtype=np.uint8)

    return run_build.Deps(listing=listing, fetch_text=fetch_text, download=download,
                          regrid=regrid, ensure_kit=lambda work: Path("/kit"),
                          now=lambda: dt.datetime(2026, 9, 27, 3, tzinfo=UTC)), calls


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
