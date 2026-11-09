# globalfeed/history6.py — the past 60 hours for a feed whose runs carry one
# hour per 6 (AIFS, IFS). pipeline/history.py needs six hourly head steps, which
# these feeds do not publish, so this keeps each run's head 6 h apart:
# state/history6/<run>.npz holding head [k][4][NY][NX] uint8 (step i is
# run_hour + 6 i) and run_hour. AIFS (a run every 6 h) keeps k = 1, its f000;
# IFS (every 12 h) k = 2, f000 and f006. The build puts the earlier runs' head
# steps ahead of its own, 6 h apart.
import datetime as dt
from pathlib import Path

import numpy as np

from pipeline.history import HISTORY_HOURS, KEEP_RUNS, hour_of

SUB = "history6"


def _dir(state):
    return Path(state) / SUB


def save_run_head6(state, run, head):
    """head: uint8 [k][4][NY][NX], the run's f000, f006, ... (k >= 1). Keeps the
    newest KEEP_RUNS runs."""
    head = np.asarray(head)
    if head.ndim != 4 or head.shape[0] < 1:
        raise ValueError(f"head must be [k][4][NY][NX] with k >= 1, got {head.shape}")
    d = _dir(state)
    d.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(d / f"{run:%Y%m%d%H}.npz", head=head.astype(np.uint8), run_hour=hour_of(run))
    for old in sorted(d.glob("*.npz"))[:-KEEP_RUNS]:
        old.unlink()


def load_history6(state, run):
    """{unix_hour: uint8 [4][NY][NX]} for each saved head step in [run - 60 h,
    run). Where two runs cover one hour, the newer run's step wins."""
    d = _dir(state)
    if not d.exists():
        return {}
    lo, hi = hour_of(run) - HISTORY_HOURS, hour_of(run)
    out = {}
    for p in sorted(d.glob("*.npz")):
        with np.load(p) as z:
            h0 = int(z["run_hour"])
            head = z["head"]
            for i in range(head.shape[0]):
                h = h0 + 6 * i
                if lo <= h < hi:
                    out[h] = head[i]
    return out
