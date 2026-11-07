# hrrr/build.py — one HRRR run -> ICL1 squares + manifest.json.
#
# Same file layout and manifest shape as the ICON feed's Pages copy
# (tiles/<YYYYMMDDHH>/<square>.icl), so the app reads it with the same
# decoder; the manifest adds "model": "hrrr" and carries no blocks, thick,
# columns or tail keys. Hours f00..last_hour, no history.
import concurrent.futures as cf
import datetime as dt
import json
import os
import time
from pathlib import Path

import numpy as np

from pipeline.encode import encode_square
from pipeline.site import build_manifest, tiles_path
from pipeline.squares import parse_name

from .grib import FIELDS, file_url, read_fields
from .grid import SQUARES, bin_index, box_average

RETRIES = 2          # extra attempts per forecast hour
ATTRIBUTION = "Cloud data: NOAA HRRR"


def last_hour(run):
    """HRRR runs the long 48 h forecast at 00/06/12/18Z, 18 h otherwise."""
    return 48 if run.hour % 6 == 0 else 18


def newest_complete_run(exists, now):
    """The newest of the last four cycle hours whose final forecast hour is
    published (its idx exists), or None. now is timezone-aware UTC."""
    top = now.replace(minute=0, second=0, microsecond=0)
    for back in range(4):
        run = top - dt.timedelta(hours=back)
        if exists(file_url(run, last_hour(run)) + ".idx"):
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


def build(run, out_dir, get, now, sleep=time.sleep):
    """Write out_dir/tiles/<YYYYMMDDHH>/<name>.icl for every SQUARES entry and
    out_dir/manifest.json. Returns the square names. Any hour that still fails
    after its retries raises: a build is all or nothing."""
    out_dir = Path(out_dir)
    last = last_hour(run)
    workers = int(os.environ.get("HRRR_WORKERS", "8"))
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        steps = list(ex.map(lambda fh: _read_step(run, fh, get, sleep), range(last + 1)))
    bins = bin_index(steps[0]["lat"], steps[0]["lon"])   # the grid is fixed for a run
    per_step = []
    for s in steps:
        by_field = [box_average(s[name], bins) for name, _ in FIELDS]
        per_step.append({n: np.stack([f[n] for f in by_field]) for n in bins})
    base_hour = int(run.timestamp() // 3600)
    hours = [base_hour + fh for fh in range(last + 1)]
    tiles = out_dir / tiles_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    names = []
    for sl, so in SQUARES:
        name = next(n for n in bins if parse_name(n) == (sl, so))
        cube = np.stack([p[name] for p in per_step])
        (tiles / f"{name}.icl").write_bytes(encode_square(sl, so, hours, cube))
        names.append(name)
    m = json.loads(build_manifest(run, now, hours, names, 0))
    m["model"] = "hrrr"
    m["attribution"] = ATTRIBUTION
    for k in ("blocks", "thick", "columns", "tail"):
        m.pop(k, None)
    (out_dir / "manifest.json").write_text(json.dumps(m, separators=(",", ":")))
    return names
