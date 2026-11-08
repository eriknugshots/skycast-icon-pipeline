# aqm/build.py — one NOAA AQM cycle (plus the cycles before it) -> PM2.5 squares.
#
# Output, same square grid as the cloud feeds:
#   manifest.json                    written last
#   tiles/<YYYYMMDDHH>/<square>.bin  zlib(uint8[step][row][col]), row 0 south,
#                                    col 0 west, 255 = no data
# A byte is round(90 * log10(1 + ug/m3)), capped at 254 (decode_pm inverts it).
# Hours run from run-23 h to run+72 h: each hour comes from the newest cycle
# that forecast it at least one hour ahead, so the past day is the model's
# own short-range hours rather than a gap.
import datetime as dt
import json
import math
import time
import zlib
from pathlib import Path

import numpy as np

from hrrr.grid import SQUARES, bin_index
from pipeline.site import run_iso, tiles_path
from pipeline.squares import DEG, SIDE, square_name

CYCLES = (6, 12)       # the cycles that run the 72 h forecast
LAST_HOUR = 72
BACK_HOURS = 23
SCALE = 90
NO_DATA = 255
RETRIES = 2
ATTRIBUTION = "Air quality: NOAA AQM"
HOUR = dt.timedelta(hours=1)


def encode_pm(p):
    if p is None or math.isnan(p):
        return NO_DATA
    return min(254, round(SCALE * math.log10(1 + max(p, 0.0))))


def decode_pm(b):
    return 10 ** (b / SCALE) - 1


def _encode_array(p):
    out = np.minimum(254, np.rint(SCALE * np.log10(1 + np.maximum(p, 0.0))))
    return np.where(np.isnan(p), NO_DATA, out).astype(np.uint8)


def box_mean(values, bins):
    """{square name: uint8[SIDE][SIDE]} — the mean of the source cells nearest
    each node (NaN cells skipped, no clip at 100), encoded; 255 where no
    usable cell falls."""
    out = {}
    for name, (idx, flat) in bins.items():
        v = values[idx]
        good = ~np.isnan(v)
        s = np.bincount(flat[good], v[good].astype(np.float64), SIDE * SIDE)
        n = np.bincount(flat[good], None, SIDE * SIDE)
        m = np.where(n > 0, s / np.maximum(n, 1), np.nan)
        out[name] = _encode_array(m).reshape(SIDE, SIDE)
    return out


def cycles_up_to(t, count=6):
    """The 06Z/12Z cycles at or before t, newest first."""
    top = t.replace(minute=0, second=0, microsecond=0)
    out = []
    while len(out) < count:
        if top.hour in CYCLES:
            out.append(top)
        top -= HOUR
    return out


def newest_complete_run(exists, now):
    """The newest 06Z/12Z cycle whose file is published (exists(run) true), or
    None. now is timezone-aware UTC."""
    for run in cycles_up_to(now, 4):
        if exists(run):
            return run
    return None


def hour_plan(run, have=None):
    """[(valid, source cycle, forecast hour)] hourly from run-23 h to run+72 h,
    each from the newest cycle in `have` (default: every 06/12Z cycle up to
    run) that is at least one hour before it. Hours no cycle can supply are
    left out, so a build missing its earlier cycles starts later and stays
    contiguous."""
    if have is None:
        have = cycles_up_to(run, 8)
    have = sorted((c for c in have if c <= run), reverse=True)
    plan = []
    for k in range(-BACK_HOURS, LAST_HOUR + 1):
        valid = run + k * HOUR
        src = next((c for c in have if c <= valid - HOUR), None)
        if src is None:
            continue
        fh = int((valid - src) / HOUR)
        if fh <= LAST_HOUR:
            plan.append((valid, src, fh))
    return plan


def _reduce(data, bins):
    return {fh: box_mean(arr, bins) for fh, arr in data["hours"].items()}


def build(run, out_dir, read, now, sleep=time.sleep):
    """Write out_dir/tiles/<YYYYMMDDHH>/<name>.bin for every SQUARES entry and
    out_dir/manifest.json; returns the square names.

    read(cycle) -> {"lat", "lon", "hours": {fh: array}} or None when that
    cycle's file is not published. The run itself must be there with every
    forecast hour 1..72; an earlier cycle that is absent only shortens the
    past. Anything else raises: a build is all or nothing."""
    out_dir = Path(out_dir)
    bins = None
    reduced = {}      # cycle -> {fh: {name: uint8}}
    missing = set()

    def load(cycle):
        nonlocal bins
        for attempt in range(RETRIES + 1):
            try:
                data = read(cycle)
                break
            except Exception:
                if attempt == RETRIES:
                    raise
                sleep(2 * (attempt + 1))
        if data is None:
            return False
        if bins is None:
            bins = bin_index(data["lat"], data["lon"])
        reduced[cycle] = _reduce(data, bins)
        return True

    if not load(run):
        raise RuntimeError(f"AQM run {run_iso(run)} is not published")
    absent = [fh for fh in range(1, LAST_HOUR + 1) if fh not in reduced[run]]
    if absent:
        raise RuntimeError(f"AQM run {run_iso(run)} lacks forecast hours {absent[:5]}")
    while True:
        plan = hour_plan(run, [c for c in cycles_up_to(run, 8) if c not in missing])
        need = sorted({src for _, src, _ in plan} - set(reduced))
        if not need:
            break
        for cycle in need:
            if not load(cycle):
                missing.add(cycle)
                break
    for _, src, fh in plan:
        if fh not in reduced[src]:
            raise RuntimeError(f"AQM cycle {run_iso(src)} lacks forecast hour {fh}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = out_dir / tiles_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    names = []
    for sl, so in SQUARES:
        name = square_name(sl, so)
        cube = np.stack([reduced[src][fh][name] for _, src, fh in plan])
        (tiles / f"{name}.bin").write_bytes(zlib.compress(cube.tobytes(), 9))
        names.append(name)
    manifest = {
        "run": run_iso(run),
        "runMs": int(run.timestamp() * 1000),
        "builtMs": int(now.timestamp() * 1000),
        "complete": True,
        "deg": DEG, "square": 5, "side": SIDE,
        "stepsMs": [int(v.timestamp() * 1000) for v, _, _ in plan],
        "squares": sorted(names),
        "tiles": tiles_path(run),
        "encoding": {"scale": SCALE},
        "attribution": ATTRIBUTION,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    return names
