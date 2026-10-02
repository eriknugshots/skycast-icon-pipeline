# pipeline/history.py — the past 60 hours, from earlier runs' first six hours.
#
# WHY. The app's window starts at local midnight YESTERDAY — at 23:59 local
# that is 48 h ago — and the run in hand may be 9 h old. A run only covers
# time from its own start, so the hours before it come from the runs before
# it: each run's hours 0–5 were the freshest forecast of those hours before
# the next run took over. Ten runs × 6 h = 60 h, which covers the worst case.
#
# WHERE. Each build writes its run's hours 0–5 to state/history/<run>.npz and
# force-pushes state/ as an orphan `state` branch; the next build clones it.
# Nothing here touches DWD: a history hour is a step this pipeline already
# regridded. A missing or partial state is not an error — the site simply
# starts earlier than the window and the app fills from the nearest step.
import datetime as dt
from pathlib import Path
import numpy as np

HISTORY_HOURS = 60
KEEP_RUNS = 10
HEAD_STEPS = 6                     # hours 0-5 of each run
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def hour_of(when):
    return int((when - _EPOCH).total_seconds() // 3600)


# `sub` names the history: "history" for the cloud squares, "columns" for
# the column feed (pipeline/columns.py), whose head is u16 codes on its
# coarser lattice — same runs, same hours, kept and pruned the same way.
def history_dir(state, sub="history"):
    return Path(state) / sub


def run_file(state, run, sub="history"):
    return history_dir(state, sub) / f"{run:%Y%m%d%H}.npz"


def run_files(state, run, sub="history"):
    """Every file holding `run`'s head: <run>.npz, or <run>.h<i>.npz when split."""
    d = history_dir(state, sub)
    return sorted(d.glob(f"{run:%Y%m%d%H}.npz")) + sorted(d.glob(f"{run:%Y%m%d%H}.h*.npz"))


def save_run_head(state, run, head, sub="history", dtype=np.uint8, split=False):
    """head: [6][fields][rows][cols] — the run's hours 0-5. For the clouds
    uint8 [6][4][NY][NX], fields low/mid/high/total. split: one file per
    hour (<run>.h0.npz … h5) — the column head at 0.25° is ~94 MB in one
    file, near GitHub's 100 MB limit on a pushed file; split, ~16 MB each."""
    if head.shape[0] != HEAD_STEPS:
        raise ValueError(f"head must hold {HEAD_STEPS} steps, got {head.shape[0]}")
    history_dir(state, sub).mkdir(parents=True, exist_ok=True)
    for old in run_files(state, run, sub):
        old.unlink()
    if not split:
        np.savez_compressed(run_file(state, run, sub), head=head.astype(dtype), run_hour=hour_of(run))
        return
    for i in range(HEAD_STEPS):
        np.savez_compressed(history_dir(state, sub) / f"{run:%Y%m%d%H}.h{i}.npz",
                            head=head[i:i + 1].astype(dtype), run_hour=hour_of(run) + i)


def runs_to_keep(run_ids, current_id):
    """The KEEP_RUNS newest run ids (YYYYMMDDHH strings), the current one included."""
    ids = sorted(set(run_ids) | {current_id})
    return ids[-KEEP_RUNS:]


def _run_id(path):
    return path.stem.split(".")[0]          # <run>.npz or <run>.h<i>.npz


def prune(state, current_id, sub="history"):
    keep = set(runs_to_keep([_run_id(p) for p in history_dir(state, sub).glob("*.npz")], current_id))
    for p in history_dir(state, sub).glob("*.npz"):
        if _run_id(p) not in keep:
            p.unlink()


def load_history_steps(state, run, sub="history", shape=None):
    """[(unix_hour, uint8 [4][NY][NX]), ...] ascending, for hours in
    [run - HISTORY_HOURS, run). Later runs win where two runs share an hour
    (they never do at 6-hourly runs with 6-hour heads, but be safe)."""
    d = history_dir(state, sub)
    if not d.exists():
        return []
    lo, hi = hour_of(run) - HISTORY_HOURS, hour_of(run)
    by_hour = {}
    for p in sorted(d.glob("*.npz")):
        with np.load(p) as z:
            base = int(z["run_hour"])
            head = z["head"]
        if shape is not None and tuple(head.shape[1:]) != tuple(shape):
            continue                       # another layout (e.g. another column stride or field list)
        for i in range(head.shape[0]):
            h = base + i
            if lo <= h < hi:
                by_hour[h] = head[i]
    return [(h, by_hour[h]) for h in sorted(by_hour)]
