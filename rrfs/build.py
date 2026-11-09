# rrfs/build.py — one RRFS run -> ICL1 squares + manifest.json.
#
# The same file layout and manifest shape as hrrr/build.py (tiles/<YYYYMMDDHH>/
# <square>.icl, "model": "rrfs", no blocks, thick, columns or tail keys): the
# app reads it with the decoder it has. RRFS runs at 00/06/12/18Z and is read
# f000..f084 hourly, no history. NOMADS only, under its request limit: a shared
# RateLimiter (rrfs_build.py wraps every request in one) and two workers.
import concurrent.futures as cf
import datetime as dt
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from pipeline.encode import encode_square
from pipeline.site import build_manifest, tiles_path

from .grib import FIELDS, file_url, read_fields
from .grid import SQUARES, bin_index, coverage, cut_squares, node_average
from pipeline.squares import square_name

RETRIES = 2          # extra attempts per forecast hour
ATTRIBUTION = "Cloud data: NOAA RRFS"
RUN_HOURS = (0, 6, 12, 18)
LAST_HOUR = 84
STEPS = tuple(range(LAST_HOUR + 1))
REQUESTS_PER_MINUTE = 90      # NOMADS's own limit could not be read; stay well under 100


class RateLimiter:
    """At most `limit` acquisitions in any `window` seconds, across threads."""

    def __init__(self, limit, window=60.0, clock=time.monotonic, sleep=time.sleep):
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self._stamps = deque()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            while True:
                now = self.clock()
                while self._stamps and self._stamps[0] <= now - self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                self.sleep(self._stamps[0] + self.window - now)


def limited(get, limiter):
    """get(url, rng=None) -> bytes, counted against the limiter first."""
    def wrapped(url, rng=None):
        limiter.acquire()
        return get(url, rng)
    return wrapped


def workers():
    return int(os.environ.get("RRFS_WORKERS", "2"))


def newest_complete_run(exists, now):
    """The newest of the last four 00/06/12/18Z cycles whose f084 idx exists, or
    None. now is timezone-aware UTC."""
    top = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
    for back in range(4):
        run = top - dt.timedelta(hours=6 * back)
        if exists(file_url(run, LAST_HOUR) + ".idx"):
            return run
    return None


def _read_step(run, fh, get, sleep):
    for attempt in range(RETRIES + 1):
        try:
            return read_fields(run, fh, get)
        except Exception:
            if attempt == RETRIES:
                raise
            sleep(2 * (attempt + 1))


def build(run, out_dir, get, now, sleep=time.sleep, fhs=None):
    """Write out_dir/tiles/<YYYYMMDDHH>/<name>.icl for every SQUARES entry and
    out_dir/manifest.json. Returns the square names. Any hour that still fails
    after its retries raises: a build is all or nothing. fhs limits the forecast
    hours (a sample run); such a manifest says complete: false."""
    out_dir = Path(out_dir)
    full = list(STEPS)
    fhs = full if fhs is None else sorted(set(int(f) for f in fhs))
    # The first hour fixes the bins for the whole run (the grid is fixed); each
    # worker then bins its own hour and hands back only the small node grids.
    first = _read_step(run, fhs[0], get, sleep)
    bins = bin_index(first["lat"], first["lon"])

    def grids(fields):
        return np.stack([node_average(fields[name], bins) for name, _ in FIELDS])

    per_step = [grids(first)]
    have, empty = coverage(per_step[0][0])
    print(f"f{fhs[0]:03d} low cloud: {have} of {have + empty} domain nodes carry a value, {empty} are 255")
    del first
    with cf.ThreadPoolExecutor(max_workers=max(1, workers())) as ex:
        per_step += list(ex.map(lambda fh: grids(_read_step(run, fh, get, sleep)), fhs[1:]))
    base_hour = int(run.timestamp() // 3600)
    hours = [base_hour + fh for fh in fhs]
    tiles = out_dir / tiles_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    cut = cut_squares(np.stack(per_step, axis=1))        # [fields][steps] -> square [steps][fields]
    names = []
    for sl, so in SQUARES:
        name = square_name(sl, so)
        cube = np.ascontiguousarray(cut[name].transpose(1, 0, 2, 3))
        (tiles / f"{name}.icl").write_bytes(encode_square(sl, so, hours, cube))
        names.append(name)
    m = json.loads(build_manifest(run, now, hours, names, 0, complete=fhs == full))
    m["model"] = "rrfs"
    m["attribution"] = ATTRIBUTION
    for k in ("blocks", "thick", "columns", "tail"):
        m.pop(k, None)
    (out_dir / "manifest.json").write_text(json.dumps(m, separators=(",", ":")))
    return names
