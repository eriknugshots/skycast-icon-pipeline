# globalfeed/history6.py — the past 60 hours for a feed whose runs carry one
# hour per 6 (AIFS). pipeline/history.py needs six hourly head steps, which
# AIFS does not publish, so this keeps just each run's f000: state/history6/
# <run>.npz holding head [1][4][NY][NX] uint8 and run_hour. The build puts the
# f000s of earlier runs ahead of its own steps, 6 h apart.
import datetime as dt
from pathlib import Path

import numpy as np

from pipeline.history import HISTORY_HOURS, KEEP_RUNS, hour_of

SUB = "history6"


def _dir(state):
    return Path(state) / SUB


def save_run_head6(state, run, head):
    """head: uint8 [1][4][NY][NX], the run's f000. Keeps the newest KEEP_RUNS runs."""
    head = np.asarray(head)
    if head.shape[0] != 1:
        raise ValueError(f"head must hold 1 step, got {head.shape[0]}")
    d = _dir(state)
    d.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(d / f"{run:%Y%m%d%H}.npz", head=head.astype(np.uint8), run_hour=hour_of(run))
    for old in sorted(d.glob("*.npz"))[:-KEEP_RUNS]:
        old.unlink()


def load_history6(state, run):
    """{unix_hour: uint8 [4][NY][NX]} for each saved f000 in [run - 60 h, run)."""
    d = _dir(state)
    if not d.exists():
        return {}
    lo, hi = hour_of(run) - HISTORY_HOURS, hour_of(run)
    out = {}
    for p in sorted(d.glob("*.npz")):
        with np.load(p) as z:
            h = int(z["run_hour"])
            if lo <= h < hi:
                out[h] = z["head"][0]
    return out
