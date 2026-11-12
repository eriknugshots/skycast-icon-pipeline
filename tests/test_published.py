# pipeline/published.py — each run's publication time, kept in the state for
# the next builds' historyRuns.
import datetime as dt
import json

from pipeline import history, published

UTC = dt.timezone.utc
RUN = dt.datetime(2026, 11, 12, 18, tzinfo=UTC)
H = 3600 * 1000


def ms(t):
    return int(t.timestamp() * 1000)


def test_record_and_list_older_runs(tmp_path):
    for back, lag_h in ((12, 4.0), (6, 3.9)):
        r = RUN - dt.timedelta(hours=back)
        published.record(tmp_path, ms(r), ms(r) + int(lag_h * H))
    published.record(tmp_path, ms(RUN), ms(RUN) + 4 * H)
    assert json.loads((tmp_path / published.FILE).read_text()) == {
        "2026111206": ms(RUN) - 12 * H + 4 * H, "2026111212": ms(RUN) - 6 * H + int(3.9 * H),
        "2026111218": ms(RUN) + 4 * H}
    # Only the runs older than the one being built: its own is the live manifest's.
    assert published.history_runs(tmp_path, RUN) == [
        {"runMs": ms(RUN) - 12 * H, "publishedMs": ms(RUN) - 8 * H},
        {"runMs": ms(RUN) - 6 * H, "publishedMs": ms(RUN) - 6 * H + int(3.9 * H)}]
    assert len(published.history_runs(tmp_path, RUN + dt.timedelta(hours=6))) == 3


def test_a_republished_run_keeps_its_latest_time(tmp_path):
    # A repair rebuild (live.py): the run first went out without its columns
    # or thickness, so all of it was out only at the second publish.
    published.record(tmp_path, ms(RUN), ms(RUN) + 4 * H)
    published.record(tmp_path, ms(RUN), ms(RUN) + 5 * H)
    assert published.load(tmp_path) == {"2026111218": ms(RUN) + 5 * H}
    published.record(tmp_path, ms(RUN), ms(RUN) + 3 * H)        # never moves earlier
    assert published.load(tmp_path) == {"2026111218": ms(RUN) + 5 * H}


def test_keeps_the_newest_runs_only(tmp_path):
    for i in range(history.KEEP_RUNS + 5):
        r = RUN - dt.timedelta(hours=6 * i)
        published.record(tmp_path, ms(r), ms(r) + 4 * H)
    kept = published.load(tmp_path)
    assert published.KEEP_RECORDS == history.KEEP_RUNS + 2
    assert len(kept) == published.KEEP_RECORDS
    assert min(kept) == (RUN - dt.timedelta(hours=6 * (published.KEEP_RECORDS - 1))).strftime("%Y%m%d%H")
    # Every run a 60 h window ahead of the newest can hold still has its time,
    # even after the newest is published again.
    published.record(tmp_path, ms(RUN), ms(RUN) + 5 * H)
    older = [r["runMs"] for r in published.history_runs(tmp_path, RUN)]
    assert older[-history.KEEP_RUNS:] == [ms(RUN) - 6 * H * i for i in range(history.KEEP_RUNS, 0, -1)]


def test_absent_or_broken_record_is_no_times(tmp_path):
    assert published.load(tmp_path) == {}
    assert published.history_runs(tmp_path, RUN) == []
    (tmp_path / published.FILE).write_text("{not json")
    assert published.load(tmp_path) == {}
    (tmp_path / published.FILE).write_text(json.dumps(
        {"2026111212": ms(RUN), "junk": 5, "2026111206": "soon", "2026111200": -1, "2026111118": True}))
    assert published.load(tmp_path) == {"2026111212": ms(RUN)}
    # A broken record is replaced, not a reason to fail.
    published.record(tmp_path, ms(RUN), ms(RUN) + H)
    assert set(published.load(tmp_path)) == {"2026111212", "2026111218"}


def test_cli_records_the_manifests_run_now(tmp_path):
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"run": "2026-11-12T18Z", "runMs": ms(RUN)}))
    before = ms(dt.datetime.now(UTC))
    assert published.main([str(tmp_path / "state"), str(man)]) == 0
    after = ms(dt.datetime.now(UTC))
    got = published.load(tmp_path / "state")
    assert list(got) == ["2026111218"] and before <= got["2026111218"] <= after
    assert published.main([]) == 2


def test_build_workflow_stamps_only_after_a_blob_publish_that_worked():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / ".github/workflows/build.yml").read_text()
    blob_step = text.split("- name: publish to Vercel Blob")[1].split("\n      - name:")[0]
    assert "id: blob" in blob_step and "continue-on-error: true" in blob_step
    state_step = text.split("- name: publish state")[1].split("\n      - name:")[0]
    gate = 'if [ "${{ steps.blob.outcome }}" = "success" ]; then'
    stamp = "python -m pipeline.published state blob/manifest.json || echo"
    assert gate in state_step and stamp in state_step
    assert state_step.index(gate) < state_step.index(stamp) < state_step.index("cd state")
