#!/usr/bin/env python3
# run_build.py — one DWD ICON run → the site. See README and pipeline/*.py.
#
# Exit codes double as outcomes for the workflow: NOTHING_TO_DO skips the
# publish step, FAILED leaves the live site as it was. A build is all or
# nothing — a square with a hole in it would read as clear sky somewhere.
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import namedtuple
from pathlib import Path
import numpy as np

from pipeline import columns, dwd, history, live, site
from pipeline.encode import encode_square, quantize, FIELD_COUNT
from pipeline.regrid import ensure_kit, regrid, lattice_levels
from pipeline.squares import all_squares, parse_name, slice_square, NX, NY

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1
ROOT = Path(__file__).resolve().parent

# lattice_levels and sleep are the column feed's (build_columns); they default
# to the real ones so a caller that predates the columns still builds.
Deps = namedtuple("Deps", "listing fetch_text download regrid ensure_kit now lattice_levels sleep",
                  defaults=(lattice_levels, time.sleep))


def _http_text(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _download(url, dst, timeout=120, attempts=3, urlopen=urllib.request.urlopen):
    """Fetch url to dst, whole: a short body (a dropped connection, a proxy
    cutting off) is retried, then raised — never handed to bunzip2 to fail
    on later, or worse, decoded as far as it goes."""
    last = None
    for _ in range(attempts):
        with urlopen(url, timeout=timeout) as r, open(dst, "wb") as f:
            want = r.headers.get("Content-Length")
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
        if want is None or got == int(want):
            return
        last = f"{url}: got {got} of {want} bytes"
    raise OSError(f"download truncated after {attempts} attempts: {last}")


REAL = Deps(listing=lambda hh, field: _http_text(dwd.listing_url(hh, field)),
            fetch_text=_http_text, download=_download, regrid=regrid, ensure_kit=ensure_kit,
            now=lambda: dt.datetime.now(dt.timezone.utc), lattice_levels=lattice_levels, sleep=time.sleep)


def live_complete_run(deps, pages_base):
    """The run the live site carries COMPLETELY (see pipeline/live.py)."""
    return live.live_complete_run(deps.fetch_text, pages_base)


def build(site, state, work, deps, pages_base, steps=None, squares=None, workers=None):
    run = dwd.newest_complete_run(deps.listing)
    if run is None:
        print("no complete run on DWD yet")
        return NOTHING_TO_DO
    full = steps is None and squares is None
    if full and live_complete_run(deps, pages_base) == dwd.run_iso(run):
        print(f"run {dwd.run_iso(run)} already published — nothing to do")
        return NOTHING_TO_DO
    steps = list(steps) if steps is not None else dwd.STEPS
    site, state, work = Path(site), Path(state), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    kit = deps.ensure_kit(work)
    if squares is not None:
        names = list(squares)
    else:
        # Every square: ICON covers the globe, and an island or a pin at sea
        # must work the same as anywhere (Erik, 2026-10-01). It fits Pages'
        # 1 GB because cover is published in QUANT_STEP steps (encode.quantize).
        names = site_squares()
    print(f"building run {dwd.run_iso(run)}: {len(steps)} steps × {FIELD_COUNT} fields, {len(names)} squares")

    # One world array per (step, field). ~4 MB each as uint8: 93 × 4 = 1.5 GB
    # in memory, well inside a runner's 16 GB. Downloads and regrids overlap.
    world = {}

    def job(step, field):
        url = dwd.file_url(run, step, field)
        dst = work / dwd.file_name(run, step, field)
        deps.download(url, dst)
        try:
            return step, field, deps.regrid(dst, kit, work, scale=1.0)
        finally:
            dst.unlink(missing_ok=True)

    workers = workers or int(os.environ.get("BUILD_WORKERS", "6"))
    try:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for step, field, arr in pool.map(lambda sf: job(*sf), [(s, f) for s in steps for f in dwd.FIELDS]):
                world[(step, field)] = arr
                print(f"  {field} +{step:03d}h ok", flush=True)
    except Exception as e:
        print(f"::error::run {dwd.run_iso(run)} failed: {e}")
        return FAILED

    # History first, then the run's own steps, one [4][NY][NX] stack per hour.
    past = history.load_history_steps(state, run)
    base_hour = history.hour_of(run)
    stacks = [(h, arr) for h, arr in past]
    for s in steps:
        stacks.append((base_hour + s, np.stack([world[(s, f)] for f in dwd.FIELDS])))
    hours = [h for h, _ in stacks]

    tiles = site / site_tiles(run)
    tiles.mkdir(parents=True, exist_ok=True)

    def write_squares(step):
        total = 0
        for name in names:
            lat, lon = parse_name(name)
            cube = np.stack([slice_square(arr, lat, lon) for _, arr in stacks])
            blob = encode_square(lat, lon, hours, quantize(cube, step))
            (tiles / f"{name}.icl").write_bytes(blob)
            total += len(blob)
        return total

    # The run's hours 0-5, for the next build's history — only when they were built.
    head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
    if len(head_steps) == history.HEAD_STEPS:
        head = np.stack([np.stack([world[(s, f)] for f in dwd.FIELDS]) for s in head_steps])
        history.save_run_head(state, run, head)
        history.prune(state, dwd.run_id(run))

    world.clear()                        # the stacks hold copies; ~1.5 GB back for the columns
    size = write_squares(QUANT_STEP)
    tail = build_tail(site, work, deps, kit, names, workers) if full else None
    cols = build_columns(site, state, work, deps, kit, run, steps, names, workers)
    site_bytes = size + _dir_bytes(site / site_tail_tiles_root()) + _dir_bytes(site / site_columns_root())
    if site_bytes > SITE_BUDGET_BYTES:
        print(f"::warning::{site_bytes / 1e6:.0f} MB is over the {SITE_BUDGET_BYTES / 1e6:.0f} MB budget: "
              f"rewriting the squares in {QUANT_FALLBACK_STEP} % steps")
        size = write_squares(QUANT_FALLBACK_STEP)
        site_bytes = size + _dir_bytes(site / site_tail_tiles_root()) + _dir_bytes(site / site_columns_root())
    if site_bytes > SITE_BUDGET_BYTES and cols is not None:
        # Still over: the clouds are the feed, the fog columns an extra. Pages
        # refuses a site over 1 GB outright, which would freeze every build.
        print(f"::warning::{site_bytes / 1e6:.0f} MB is still over the budget: leaving the columns out")
        shutil.rmtree(site / site_columns_root(), ignore_errors=True)
        cols = None
        site_bytes = size + _dir_bytes(site / site_tail_tiles_root())
    print(f"site: {site_bytes / 1e6:.0f} MB in squares, tail and columns")
    (site / "manifest.json").write_text(site_manifest(run, deps.now(), hours, names, len(past), full, tail, cols))
    print(f"built {len(names)} squares, {len(hours)} steps ({len(past)} history)")
    return BUILT


# Pages refuses a site over 1 GB; leave room for the manifest and a tail.
SITE_BUDGET_BYTES = 900 * 1000 * 1000
QUANT_STEP = 2
QUANT_FALLBACK_STEP = 4


def site_squares():
    """Every 5° square on the globe (squares.all_squares)."""
    return all_squares()


def _dir_bytes(path):
    path = Path(path)
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def site_tail_tiles_root():
    return "tail"


def site_tiles(run):
    return site.tiles_path(run)


def site_manifest(run, built, hours, names, history_steps, complete, tail=None, columns=None):
    return site.build_manifest(run, built, hours, names, history_steps, complete, tail, columns)


def site_tail_tiles(run):
    return site.tail_path(run)


def build_tail(site_dir, work, deps, kit, names, workers):
    """The tail: hours 123-144 of the newest whole 00Z/12Z run, one .icl per
    square under tail/<run>/, for SkyCast's days 4-5. DWD keeps each run
    hour's newest run online, so any build can make it — nothing is carried
    between builds. Returns the manifest's `tail` entry, or None; a failure
    here never fails the build."""
    try:
        run = dwd.newest_complete_run(deps.listing, run_hours=dwd.TAIL_RUN_HOURS, steps=dwd.TAIL_STEPS)
        if run is None:
            print("::warning::no whole 00Z/12Z run for the tail")
            return None
        world = {}

        def job(step, field):
            dst = work / dwd.file_name(run, step, field)
            deps.download(dwd.file_url(run, step, field), dst)
            try:
                return step, field, deps.regrid(dst, kit, work, scale=1.0)
            finally:
                dst.unlink(missing_ok=True)

        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for step, field, arr in pool.map(lambda sf: job(*sf), [(s, f) for s in dwd.TAIL_STEPS for f in dwd.FIELDS]):
                world[(step, field)] = arr
        base = history.hour_of(run)
        hours = [base + s for s in dwd.TAIL_STEPS]
        stacks = [np.stack([world[(s, f)] for f in dwd.FIELDS]) for s in dwd.TAIL_STEPS]
        tiles = Path(site_dir) / site_tail_tiles(run)
        tiles.mkdir(parents=True, exist_ok=True)
        for name in names:
            lat, lon = parse_name(name)
            cube = np.stack([slice_square(arr, lat, lon) for arr in stacks])
            (tiles / f"{name}.icl").write_bytes(encode_square(lat, lon, hours, quantize(cube, QUANT_STEP)))
    except Exception as e:
        print(f"::warning::tail failed: {e}")
        return None
    print(f"tail: run {dwd.run_iso(run)}, {len(hours)} steps, {len(names)} squares")
    return {"run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
            "stepsMs": [h * 3600 * 1000 for h in hours], "tiles": site_tail_tiles(run)}


# ---- The column feed (docs/column-feed-design.md) ---------------------------
# Every COLUMN_STRIDE-th node of the 0.125° grid: 4 → 0.5°, 11 nodes a side.
COLUMN_STRIDE = 4
# DWD lists a run's fields over ~20 minutes; the cloud fields that start a
# build are not the column fields. Wait this long for the rest, then publish
# the clouds without columns rather than hold the feed back.
COLUMN_WAIT_S = 15 * 60
COLUMN_POLL_S = 60
G = 9.80665                              # FI is geopotential (m² s⁻²); GH = FI / g
K0 = 273.15


def site_columns_root():
    return "columns"


def site_columns_tiles(run):
    return site.columns_path(run)


def column_keys():
    """The dynamic fields' keys, in field_spec's (body) order."""
    keys = []
    for p in dwd.COLUMN_PLEVELS:
        keys += [("T", p), ("RELHUM", p), ("FI", p)]
    return keys + [("T_2M", None), ("RELHUM_2M", None), ("T_80M", None)]


def column_spec():
    return columns.field_spec(dwd.COLUMN_PLEVELS)


def _to_codes(key, arr):
    """A physical world/lattice field (DWD units) → u16 codes, app units."""
    field = key[0]
    if field in ("T", "T_2M", "T_80M"):
        return columns.quantize(arr - K0, *columns._QUANT[columns.T])
    if field in ("RELHUM", "RELHUM_2M"):
        return columns.quantize(arr, *columns._QUANT[columns.RH])
    if field == "FI":
        return columns.quantize(arr / G, *columns._QUANT[columns.GH])
    raise KeyError(field)


def t80_weights(hhl, hsurf):
    """hhl: {half level: lattice height MSL}, hsurf: lattice MSL →
    {model level: weight lattice}, summing to 1 at each node, such that
    T(80 m above ground) = Σ weight · T(level). At each node the two adjacent
    levels of dwd.COLUMN_MLEVELS that bracket 80 m are used, linear in height
    (a full level sits midway between its half levels); where 80 m is above
    or below them all, the nearest level alone — clamped, never
    extrapolated. NaN where a height is missing."""
    levels = sorted(dwd.COLUMN_MLEVELS)                       # top first: 117 is above 118
    z = {lv: (hhl[lv] + hhl[lv + 1]) / 2 - hsurf for lv in levels}
    w = {lv: np.zeros(np.shape(hsurf)) for lv in levels}
    done = np.zeros(np.shape(hsurf), dtype=bool)
    pairs = list(zip(levels, levels[1:]))                     # (upper, lower), top pair first
    for k, (up, lo) in enumerate(reversed(pairs)):            # bottom pair first
        top = k == len(pairs) - 1
        here = ~done & ((z[up] >= 80.0) | top)
        with np.errstate(invalid="ignore", divide="ignore"):
            f = np.clip((80.0 - z[lo]) / (z[up] - z[lo]), 0.0, 1.0)
        w[up] = np.where(here, f, w[up])
        w[lo] = np.where(here, 1.0 - f, w[lo])
        done |= here
    bad = np.zeros(np.shape(hsurf), dtype=bool)
    for lv in levels:
        bad |= ~np.isfinite(z[lv])
    return {lv: np.where(bad, np.nan, w[lv]) for lv in levels}


def _wait_for_column_files(deps, run, steps):
    waited = 0
    while True:
        missing = dwd.missing_column_files(deps.listing, run, steps)
        if not missing:
            return True
        if waited >= COLUMN_WAIT_S:
            print(f"::warning::columns: {len(missing)} files of run {dwd.run_iso(run)} still not listed "
                  f"after {waited // 60} min (first: {missing[0]}) — publishing without columns")
            return False
        deps.sleep(COLUMN_POLL_S)
        waited += COLUMN_POLL_S


# Seconds spent downloading and remapping column files, summed over the
# workers; build_columns prints them so a build's log shows where time goes.
_CLOCK = {}
_CLOCK_LOCK = threading.Lock()


def _clock(what, t0):
    with _CLOCK_LOCK:
        _CLOCK[what] = _CLOCK.get(what, 0.0) + time.monotonic() - t0


def _fetch_levels(deps, kit, work, names, run, field, stride):
    """Download DWD files of ONE parameter → {level: lattice array}: one cdo
    decode for all of them, then DWD's weights at the lattice's points."""
    paths = []
    try:
        for n in names:
            dst = Path(work) / n
            t0 = time.monotonic()
            deps.download(dwd.dir_url(run, field, n), dst)
            _clock("download", t0)
            paths.append(dst)
        t0 = time.monotonic()
        out = deps.lattice_levels(paths, kit, work, stride)
        _clock("remap", t0)
        return out
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


def _only(levels):
    if len(levels) != 1:
        raise ValueError(f"expected one level, got {sorted(levels, key=str)}")
    return next(iter(levels.values()))


def column_statics(deps, kit, work, run, stride):
    """(HSURF lattice codes [1][LY][LX], {model level: T80 weight lattice}) — once per run."""
    hsurf = _only(_fetch_levels(deps, kit, work, [dwd.invariant_file_name(run, "HSURF")], run, "HSURF", stride))
    hhl = {}
    for lv in dwd.COLUMN_HHL_LEVELS:          # one cdo call each: no level axis to trust
        name = dwd.invariant_file_name(run, "HHL", lv)
        hhl[lv] = _only(_fetch_levels(deps, kit, work, [name], run, "HHL", stride))
    static = columns.quantize(hsurf, *columns._QUANT[columns.HSURF])[None]
    return static, t80_weights(hhl, hsurf)


def column_step(deps, kit, work, run, step, group, stride, w80):
    """One step's share of the column fields: {key: lattice u16 codes}."""
    if group in dwd.COLUMN_PFIELDS:
        names = [dwd.pressure_file_name(run, step, lv, group) for lv in dwd.COLUMN_PLEVELS]
        levels = _fetch_levels(deps, kit, work, names, run, group, stride)
        if sorted(levels) != sorted(dwd.COLUMN_PLEVELS):
            raise ValueError(f"{group} +{step}: levels {sorted(levels, key=str)}, wanted {dwd.COLUMN_PLEVELS}")
        return {(group, lv): _to_codes((group, lv), levels[lv]) for lv in dwd.COLUMN_PLEVELS}
    if group in dwd.COLUMN_SFIELDS:
        arr = _only(_fetch_levels(deps, kit, work, [dwd.file_name(run, step, group)], run, group, stride))
        return {(group, None): _to_codes((group, None), arr)}
    if group == "T_80M":
        t80 = 0.0
        for lv in dwd.COLUMN_MLEVELS:          # one cdo call each: no level axis to trust
            t = _only(_fetch_levels(deps, kit, work, [dwd.model_file_name(run, step, lv, "T")], run, "T", stride))
            t80 = t80 + w80[lv] * t
        return {("T_80M", None): _to_codes(("T_80M", None), t80)}
    raise KeyError(group)


def fetch_columns(deps, kit, work, run, steps, workers, stride):
    """Download and remap every column field of `run` at `steps`: (static
    u16 [1][LY][LX], [(unix hour, u16 [fields][LY][LX])] in time order)."""
    work = Path(work)
    with _CLOCK_LOCK:
        _CLOCK.clear()
    static, w80 = column_statics(deps, kit, work, run, stride)
    keys = column_keys()
    groups = list(dwd.COLUMN_PFIELDS) + list(dwd.COLUMN_SFIELDS) + ["T_80M"]
    jobs = [(s, g) for s in steps for g in groups]
    got = {s: {} for s in steps}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(column_step, deps, kit, work, run, s, g, stride, w80) for s, g in jobs]
        for (s, g), fut in zip(jobs, futs):
            got[s].update(fut.result())
    base = history.hour_of(run)
    own = []
    for s in steps:
        fields = got.pop(s)
        own.append((base + s, np.stack([fields[k] for k in keys])))
    return static, own


def write_columns(site_dir, run, names, stride, static, stacks, workers=1):
    """One .icc per square under columns/<run>/; returns the bytes written.
    zlib releases the GIL, so squares encode in parallel threads."""
    spec = column_spec()
    hours = [h for h, _ in stacks]
    tiles = Path(site_dir) / site_columns_tiles(run)
    tiles.mkdir(parents=True, exist_ok=True)

    def one(name):
        lat, lon = parse_name(name)
        cube = np.stack([columns.slice_nodes(c, lat, lon, stride) for _, c in stacks])
        blob = columns.encode_columns(lat, lon, stride, hours, spec, cube,
                                      columns.slice_nodes(static, lat, lon, stride))
        (tiles / f"{name}.icc").write_bytes(blob)
        return len(blob)

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return sum(pool.map(one, names))


def build_columns(site_dir, state, work, deps, kit, run, steps, names, workers, stride=None):
    """The column feed: per square, every column field at every step (history
    first), one .icc under columns/<run>/. Returns the manifest's `columns`
    entry, or None; a failure here never fails the build — the app keeps its
    Open-Meteo profile."""
    stride = stride or COLUMN_STRIDE
    t0 = time.monotonic()
    try:
        if not _wait_for_column_files(deps, run, steps):
            return None
        static, own = fetch_columns(deps, kit, work, run, steps, workers, stride)
        past = history.load_history_steps(state, run, sub="columns", shape=own[0][1].shape)
        stacks = past + own
        hours = [h for h, _ in stacks]
        base = history.hour_of(run)
        head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
        if len(head_steps) == history.HEAD_STEPS:
            by_hour = dict(own)
            history.save_run_head(state, run, np.stack([by_hour[base + s] for s in head_steps]),
                                  sub="columns", dtype=np.uint16)
            history.prune(state, dwd.run_id(run), sub="columns")
        total = write_columns(site_dir, run, names, stride, static, stacks, workers)
    except Exception as e:
        print(f"::warning::columns failed: {type(e).__name__}: {e}")
        shutil.rmtree(Path(site_dir) / site_columns_root(), ignore_errors=True)
        return None
    print(f"columns: {len(names)} squares, {len(hours)} steps ({len(past)} history), "
          f"{total / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s (worker-seconds: "
          + ", ".join(f"{k} {v:.0f}" for k, v in sorted(_CLOCK.items())) + ")")
    return {"format": "ICC1", "run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
            "stepsMs": [h * 3600 * 1000 for h in hours], "historySteps": len(past),
            "spacing": columns.spacing_deg(stride), "side": columns.side(stride),
            "tiles": site_columns_tiles(run)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir", default="site")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--pages-base", default=None)
    ap.add_argument("--steps", default=None, help="comma list of forecast hours (testing)")
    ap.add_argument("--squares", default=None, help="comma list of square names (testing)")
    a = ap.parse_args()
    steps = [int(s) for s in a.steps.split(",")] if a.steps else None
    squares = a.squares.split(",") if a.squares else None
    sys.exit(build(a.site_dir, a.state_dir, a.work_dir, REAL, a.pages_base, steps, squares))


if __name__ == "__main__":
    main()
