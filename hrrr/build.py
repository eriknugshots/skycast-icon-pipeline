# hrrr/build.py — one HRRR run -> ICL1 squares + manifest.json.
#
# Same file layout and manifest shape as the ICON feed's Pages copy
# (tiles/<YYYYMMDDHH>/<square>.icl), so the app reads it with the same
# decoder; the manifest adds "model": "hrrr" and carries no blocks, columns
# or tail keys. Hours f00..last_hour, no history.
#
# Cloud thickness: thick/<YYYYMMDDHH>/<square>.ict (ICT1, ice then water, the
# ICON feed's file and manifest shape) over the same hours as the squares.
# It is read after the cover; if any hour of it fails the build publishes the
# cover without a "thick" key rather than failing (run_build.build_thick's rule).
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from pipeline import blocks, thick
from pipeline.dwd import run_iso
from pipeline.encode import encode_square
from pipeline.site import build_manifest, thick_path, tiles_path
from pipeline.squares import SIDE, parse_name

from .grib import FIELDS, file_url, read_fields, read_thick
from .grid import SQUARES, bin_index, box_average, box_mean

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


def _read_thick_step(run, fh, get, sleep):
    for attempt in range(RETRIES + 1):
        try:
            return read_thick(run, fh, get)
        except Exception:
            if attempt == RETRIES:
                raise
            sleep(2 * (attempt + 1))


def build_thick(run, out_dir, get, sleep, fhs, bins, workers):
    """Write out_dir/thick/<YYYYMMDDHH>/<name>.ict for every SQUARES entry over
    the hours fhs and return the manifest's `thick` entry, or None. Any failure
    removes what was written and returns None: the cover goes out without
    thickness, never a failed forecast."""
    t0 = time.monotonic()
    out = Path(out_dir) / thick_path(run)

    def stack(fh):
        d = _read_thick_step(run, fh, get, sleep)
        both = [box_mean(d[k], bins) for k in ("ice", "water")]
        return {n: np.stack([thick.thick_bytes(b[n]) for b in both]) for n in bins}

    ex = cf.ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        try:
            per_step = [f.result() for f in [ex.submit(stack, fh) for fh in fhs]]
        except BaseException:
            ex.shutdown(wait=True, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
        base_hour = int(run.timestamp() // 3600)
        hours = [base_hour + fh for fh in fhs]
        out.mkdir(parents=True, exist_ok=True)
        total = 0
        for sl, so in SQUARES:
            name = next(n for n in bins if parse_name(n) == (sl, so))
            cube = np.stack([p[name] for p in per_step])
            data = blocks.encode_blocks(b"ICT1", sl, so, hours, cube)
            (out / f"{name}.ict").write_bytes(data)
            total += len(data)
    except Exception as e:
        print(f"::warning::thickness failed: {type(e).__name__}: {e}")
        shutil.rmtree(Path(out_dir) / "thick", ignore_errors=True)
        return None
    print(f"thickness: {len(SQUARES)} squares, {len(fhs)} steps, "
          f"{total / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s")
    return {"format": "ICT1", "run": run_iso(run), "runMs": int(run.timestamp() * 1000),
            "tiles": thick_path(run)}


def build(run, out_dir, get, now, sleep=time.sleep, fhs=None):
    """Write out_dir/tiles/<YYYYMMDDHH>/<name>.icl for every SQUARES entry,
    out_dir/thick/... when thickness could be read, and out_dir/manifest.json.
    Returns the square names. Any cover hour that still fails after its retries
    raises: a build is all or nothing. fhs limits the forecast hours (a sample
    run); such a manifest says complete: false."""
    out_dir = Path(out_dir)
    last = last_hour(run)
    fhs = list(range(last + 1)) if fhs is None else sorted(set(int(f) for f in fhs))
    workers = int(os.environ.get("HRRR_WORKERS", "8"))
    # Hour 0 first: its lat/lon fix the bins for the whole run (the grid is
    # fixed), then each worker bins its own hour and hands back only the small
    # per-square stack, so peak memory is ~workers x one hour, not 49 hours.
    first = _read_step(run, fhs[0], get, sleep)
    bins = bin_index(first["lat"], first["lon"])

    def stack(fields):
        by_field = [box_average(fields[name], bins) for name, _ in FIELDS]
        return {n: np.stack([f[n] for f in by_field]) for n in bins}

    per_step = [stack(first)]
    del first
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        per_step += list(ex.map(lambda fh: stack(_read_step(run, fh, get, sleep)), fhs[1:]))
    base_hour = int(run.timestamp() // 3600)
    hours = [base_hour + fh for fh in fhs]
    tiles = out_dir / tiles_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    names = []
    for sl, so in SQUARES:
        name = next(n for n in bins if parse_name(n) == (sl, so))
        cube = np.stack([p[name] for p in per_step])
        (tiles / f"{name}.icl").write_bytes(encode_square(sl, so, hours, cube))
        names.append(name)
    thickness = build_thick(run, out_dir, get, sleep, fhs, bins, workers)
    m = json.loads(build_manifest(run, now, hours, names, 0, complete=fhs == list(range(last + 1)),
                                  thick=thickness))
    m["model"] = "hrrr"
    m["attribution"] = ATTRIBUTION
    for k in ("blocks", "columns", "tail") + (() if thickness else ("thick",)):
        m.pop(k, None)
    (out_dir / "manifest.json").write_text(json.dumps(m, separators=(",", ":")))
    return names
