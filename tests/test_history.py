import datetime as dt
import numpy as np
from pipeline.history import (HISTORY_HOURS, KEEP_RUNS, runs_to_keep, save_run_head,
                              load_history_steps, hour_of)
from pipeline.squares import NX, NY

UTC = dt.timezone.utc
R = lambda d, h: dt.datetime(2026, 9, d, h, tzinfo=UTC)


def test_constants():
    assert HISTORY_HOURS == 60 and KEEP_RUNS == 10


def test_hour_of_is_unix_hours():
    assert hour_of(dt.datetime(1970, 1, 1, 5, tzinfo=UTC)) == 5
    assert hour_of(R(27, 0)) == 497_352


def test_runs_to_keep_is_the_ten_newest_including_the_current():
    have = [f"{r:%Y%m%d%H}" for r in [R(24, 0), R(24, 6), R(24, 12), R(24, 18), R(25, 0), R(25, 6),
                                     R(25, 12), R(25, 18), R(26, 0), R(26, 6), R(26, 12), R(26, 18)]]
    keep = runs_to_keep(have + ["2026092700"], "2026092700")
    assert keep == sorted(have)[-9:] + ["2026092700"]
    assert len(keep) == KEEP_RUNS


def test_save_and_load_history_steps(tmp_path):
    state = tmp_path / "state"
    # Two earlier runs, 6 h apart, each with hours 0-5.
    for run, val in [(R(26, 12), 12), (R(26, 18), 18)]:
        head = np.full((6, 4, NY, NX), val, dtype=np.uint8)
        save_run_head(state, run, head)
    # The current run is 27 00Z: its history window is [26 12Z, 27 00Z).
    steps = load_history_steps(state, R(27, 0))
    hours = [h for h, _ in steps]
    assert hours == [hour_of(R(26, 12)) + i for i in range(6)] + [hour_of(R(26, 18)) + i for i in range(6)]
    assert steps[0][1][0, 0, 0] == 12 and steps[-1][1][0, 0, 0] == 18
    assert steps[0][1].shape == (4, NY, NX)


def test_load_history_ignores_hours_at_or_after_the_run_and_older_than_the_window(tmp_path):
    state = tmp_path / "state"
    save_run_head(state, R(24, 0), np.zeros((6, 4, NY, NX), dtype=np.uint8))   # 72 h old: out
    save_run_head(state, R(27, 0), np.zeros((6, 4, NY, NX), dtype=np.uint8))   # the run itself: out
    assert load_history_steps(state, R(27, 0)) == []


def test_load_history_with_no_state_is_empty(tmp_path):
    assert load_history_steps(tmp_path / "nope", R(27, 0)) == []


def test_the_column_history_is_its_own_u16_store_beside_the_clouds(tmp_path):
    from pipeline.history import prune, run_file
    clouds = np.full((6, 4, 3, 5), 7, dtype=np.uint8)
    cols = np.full((6, 24, 3, 5), 40000, dtype=np.uint16)
    save_run_head(tmp_path, R(27, 0), clouds)
    save_run_head(tmp_path, R(27, 0), cols, sub="columns", dtype=np.uint16)
    assert run_file(tmp_path, R(27, 0)).exists() and run_file(tmp_path, R(27, 0), "columns").exists()
    got = load_history_steps(tmp_path, R(27, 6), sub="columns", shape=(24, 3, 5))
    assert [h for h, _ in got] == [hour_of(R(27, 0)) + i for i in range(6)]
    assert got[0][1].dtype == np.uint16 and int(got[0][1].max()) == 40000
    assert load_history_steps(tmp_path, R(27, 6))[0][1].dtype == np.uint8
    # A head of another layout (an older stride or field list) is skipped.
    assert load_history_steps(tmp_path, R(27, 6), sub="columns", shape=(24, 6, 10)) == []
    prune(tmp_path, "2026092700", sub="columns")
    assert run_file(tmp_path, R(27, 0)).exists()


def test_a_split_head_is_one_file_per_hour_and_reads_and_prunes_like_one(tmp_path):
    from pipeline.history import prune, run_files, HEAD_STEPS
    cols = np.arange(6 * 2 * 3 * 5, dtype=np.uint16).reshape(6, 2, 3, 5)
    save_run_head(tmp_path, R(27, 0), cols, sub="columns", dtype=np.uint16, split=True)
    files = run_files(tmp_path, R(27, 0), "columns")
    assert [p.name for p in files] == [f"2026092700.h{i}.npz" for i in range(HEAD_STEPS)]
    got = load_history_steps(tmp_path, R(27, 6), sub="columns", shape=(2, 3, 5))
    assert [h for h, _ in got] == [hour_of(R(27, 0)) + i for i in range(HEAD_STEPS)]
    assert all(np.array_equal(a, cols[i]) for i, (_, a) in enumerate(got))
    # Saving the run again replaces its files; prune counts runs, not files.
    save_run_head(tmp_path, R(27, 0), cols, sub="columns", dtype=np.uint16, split=True)
    assert len(run_files(tmp_path, R(27, 0), "columns")) == HEAD_STEPS
    for k in range(1, KEEP_RUNS + 2):
        save_run_head(tmp_path, R(27, 0) + dt.timedelta(hours=6 * k), cols, sub="columns", dtype=np.uint16, split=True)
    newest = R(27, 0) + dt.timedelta(hours=6 * (KEEP_RUNS + 1))
    prune(tmp_path, f"{newest:%Y%m%d%H}", sub="columns")
    left = sorted({p.stem.split(".")[0] for p in (tmp_path / "columns").glob("*.npz")})
    assert len(left) == KEEP_RUNS and len(list((tmp_path / "columns").glob("*.npz"))) == KEEP_RUNS * HEAD_STEPS
    assert left[-1] == f"{newest:%Y%m%d%H}"
