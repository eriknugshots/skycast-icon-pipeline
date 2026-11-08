# globalfeed/build.py — one GFS or AIFS run -> the feed's files.
#
# Same layout the ICON build writes for cloud cover, so the app reads it with
# the decoders it already has:
#   tiles/<run>/<sq>.icl   ICL1, history + main steps
#   blocks/<run>/<sq>.icb  ICL2, the same steps in day blocks
#   tail/<run>/<sq>.icl    ICL1, the tail steps alone
#   manifest.json
# Memory follows hrrr/build.py: each worker reads one hour, puts it on the
# feed's grid and cuts it straight into per-square uint8 [4][41][41] slices,
# so only the small slices accumulate, never a stack of world arrays.
import concurrent.futures as cf
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np

from pipeline import blocks, history
from pipeline.dwd import run_id, run_iso
from pipeline.encode import encode_square, quantize
from pipeline.site import blocks_path, build_manifest, tail_path, tiles_path
from pipeline.squares import all_squares, parse_name, slice_square

from . import history6
from .grib import read_step
from .latlon import to_feed

RETRIES = 2          # extra attempts per forecast hour
QUANT_STEP = 2       # the same step the ICON Blob copy uses (run_build.QUANT_STEP)
SYNOPTIC = (0, 6, 12, 18)


def newest_complete_run(model, exists, now):
    """The newest of the last four synoptic cycles at or before now whose last
    tail step is published, or None."""
    top = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
    for back in range(4):
        run = top - dt.timedelta(hours=6 * back)
        if exists(model.index_url(run, model.tail_steps[-1])):
            return run
    return None


def _read(model, run, fh, get, sleep):
    for attempt in range(RETRIES + 1):
        try:
            return read_step(model, run, fh, get)
        except Exception:
            if attempt == RETRIES:
                raise
            sleep(2 * (attempt + 1))


def _world(model, step):
    return np.stack([to_feed(step["lat"], step["lon"], step[f if isinstance(f, str) else f[0]])
                     for f in model.fields])


def _cut(world, names):
    return {n: slice_square(world, *parse_name(n)) for n in names}


def _hours(model, run, steps, get, sleep, names, keep=()):
    """[{name: uint8 [4][41][41]}] per step, in order, read by a worker pool;
    plus {step: world} for the steps listed in keep."""
    kept = {}

    def one(fh):
        world = _world(model, _read(model, run, fh, get, sleep))
        return fh, _cut(world, names), (world if fh in keep else None)

    out = []
    with cf.ThreadPoolExecutor(max_workers=max(1, model.workers)) as ex:
        for fh, cut, world in ex.map(one, steps):
            out.append(cut)
            if world is not None:
                kept[fh] = world
    return out, kept


def _write(out_dir, rel, names, hours, per_hour, suffix, encode):
    d = Path(out_dir) / rel
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        cube = quantize(np.stack([p[n] for p in per_hour]), QUANT_STEP)
        lat, lon = parse_name(n)
        (d / f"{n}{suffix}").write_bytes(encode(lat, lon, hours, cube))


def build(model, run, out_dir, get, now, state, sleep=time.sleep):
    """Write the run into out_dir; returns the square names. A step that still
    fails after its retries raises: a build is all or nothing."""
    names = all_squares()
    base = history.hour_of(run)
    gfs_like = model.name == "gfs"

    head_steps = list(range(history.HEAD_STEPS)) if gfs_like else [0]
    keep = tuple(s for s in head_steps if s in model.main_steps)
    own, kept = _hours(model, run, model.main_steps, get, sleep, names, keep=keep)
    own_hours = [base + s for s in model.main_steps]

    # History first (unreadable history costs the past, never the run).
    past, past_hours = [], []
    try:
        if gfs_like:
            found = history.load_history_steps(state, run, sub=model.name)
        else:
            found = sorted(history6.load_history6(state, run).items())
        for h, arr in found:
            past_hours.append(h)
            past.append(_cut(arr, names))
    except Exception as e:
        print(f"::warning::history unreadable, building without it: {type(e).__name__}: {e}")
        past, past_hours = [], []

    if len(keep) == len(head_steps):
        head = np.stack([kept[s] for s in head_steps])
        if gfs_like:
            history.save_run_head(state, run, head, sub=model.name)
            history.prune(state, run_id(run), sub=model.name)
        else:
            history6.save_run_head6(state, run, head)
    kept.clear()

    hours = past_hours + own_hours
    per_hour = past + own
    _write(out_dir, tiles_path(run), names, hours, per_hour, ".icl", encode_square)
    _write(out_dir, blocks_path(run), names, hours, per_hour, ".icb",
           lambda lat, lon, h, cube: blocks.encode_blocks(b"ICL2", lat, lon, h, cube))
    del per_hour, past, own

    tail_hours = [base + s for s in model.tail_steps]
    tail_cut, _ = _hours(model, run, model.tail_steps, get, sleep, names)
    _write(out_dir, tail_path(run), names, tail_hours, tail_cut, ".icl", encode_square)
    tail = {"run": run_iso(run), "runMs": int(run.timestamp() * 1000),
            "stepsMs": [h * 3600 * 1000 for h in tail_hours], "tiles": tail_path(run)}

    m = json.loads(build_manifest(run, now, hours, names, len(past_hours), True, tail=tail,
                                  blocks=blocks_path(run)))
    for k in ("columns", "thick"):
        m.pop(k, None)
    m["model"] = model.name
    m["attribution"] = model.attribution
    (Path(out_dir) / "manifest.json").write_text(json.dumps(m, separators=(",", ":")))
    return names
