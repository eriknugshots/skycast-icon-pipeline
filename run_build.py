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
import http.client
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path
import numpy as np

from pipeline import blocks, cluefile, clues, columns, dwd, history, live, site, thick
from pipeline.encode import encode_square, quantize, FIELD_COUNT
from pipeline.regrid import ensure_kit, regrid, lattice_levels, regrid_values
from pipeline.squares import all_squares, parse_name, slice_square, NX, NY

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1
ROOT = Path(__file__).resolve().parent

# lattice_levels and sleep are the column feed's (build_columns), regrid_values
# the thickness feed's; they default to the real ones so a caller that predates
# them still builds.
Deps = namedtuple("Deps", "listing fetch_text download regrid ensure_kit now lattice_levels sleep regrid_values validate",
                  defaults=(lattice_levels, time.sleep, regrid_values, None))


def _http_text(url, timeout=60):
    return live.http_text(url, timeout=timeout)


def _download(url, dst, timeout=120, attempts=4, urlopen=urllib.request.urlopen, sleep=time.sleep):
    """Fetch url to dst, whole: a short body (a proxy cutting off) or a
    connection DWD drops outright (RemoteDisconnected, a reset, a timeout) is
    retried after a short pause, then raised — never handed to bunzip2 to
    fail on later, or worse, decoded as far as it goes. A 404 is not retried:
    the file is not there. (2026-10-28: one dropped connection among the
    column feed's thousands of files cost the 18Z run its fog data.)"""
    last = None
    for i in range(attempts):
        if i:
            sleep(2 * i)
        try:
            with urlopen(url, timeout=timeout) as r, open(dst, "wb") as f:
                want = r.headers.get("Content-Length")
                got = 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = f"{url}: HTTP {e.code}"
            continue
        except (OSError, http.client.HTTPException) as e:
            last = f"{url}: {type(e).__name__}: {e}"
            continue
        if want is None or got == int(want):
            return
        last = f"{url}: got {got} of {want} bytes"
    raise OSError(f"download failed after {attempts} attempts: {last}")


REAL = Deps(listing=lambda hh, field: _http_text(dwd.listing_url(hh, field)),
            fetch_text=_http_text, download=_download, regrid=regrid, ensure_kit=ensure_kit,
            now=lambda: dt.datetime.now(dt.timezone.utc), lattice_levels=lattice_levels, sleep=time.sleep,
            regrid_values=regrid_values, validate=None)


def live_complete_run(deps, pages_base):
    """The run the live feed carries COMPLETELY (see pipeline/live.py)."""
    return live.live_complete_run(deps.fetch_text, pages_base)


# A world field no real ICON run can produce: a broken or truncated file that
# still decoded. CLCT is total cover; its plain mean over the 0.125° grid sits
# near 65 % (the cloudy poles weigh in) and never near 0 or 100.
CLCT_MEAN_RANGE = (20.0, 95.0)


def world_problems(world):
    """[problem, …] for the downloaded cloud fields {(step, field): uint8
    world}: any field the same value everywhere, or a step whose total cover
    averages outside CLCT_MEAN_RANGE. Empty when the run looks real."""
    out = []
    for (step, field), arr in sorted(world.items()):
        if arr.size == 0 or int(arr.max()) == int(arr.min()):
            out.append(f"{field} +{step}h is {int(arr.max()) if arr.size else 'empty'} everywhere")
        elif field == "CLCT":
            m = float(arr.mean())
            if not CLCT_MEAN_RANGE[0] <= m <= CLCT_MEAN_RANGE[1]:
                out.append(f"CLCT +{step}h averages {m:.1f} %")
    return out


def build(site, state, work, deps, pages_base, steps=None, squares=None, workers=None, blob=None):
    """site: the Pages tree (squares, tail, manifest). blob: files for Vercel
    Blob only — the day blocks, the thickness, the column feed and a manifest
    that names them (default: a `blob` directory beside `site`).
    publish/blob.mjs uploads site, then blob over it; Pages never sees blob,
    so none of them count against its 1 GB."""
    run = dwd.newest_complete_run(deps.listing)
    if run is None:
        print("no complete run on DWD yet")
        return NOTHING_TO_DO
    full = steps is None and squares is None
    if full and live.run_is_done(deps.fetch_text, pages_base, dwd.run_iso(run)):
        print(f"run {dwd.run_iso(run)} already published — nothing to do")
        return NOTHING_TO_DO
    # A rebuild of the live run (it went out with a piece missing) counts
    # toward live.MAX_REPAIRS.
    repairs = live.repairs_for(deps.fetch_text, pages_base, dwd.run_iso(run)) if full else 0
    if repairs:
        print(f"run {dwd.run_iso(run)} is live without every part — rebuilding it (repair {repairs})")
    steps = list(steps) if steps is not None else dwd.STEPS
    site, state, work = Path(site), Path(state), Path(work)
    blob = Path(blob) if blob is not None else site.parent / "blob"
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
    # Never publish an impossible field over a good run: FAILED leaves the
    # live feed as it was, and the next tick tries again.
    problems = (deps.validate or world_problems)(world)
    if problems:
        print(f"::error::run {dwd.run_iso(run)} looks broken, not publishing: " + "; ".join(problems[:5])
              + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        return FAILED

    # History first, then the run's own steps, one [4][NY][NX] stack per hour.
    # Unreadable history costs the past hours, never the run.
    try:
        past = history.load_history_steps(state, run)
    except Exception as e:
        print(f"::warning::history unreadable, building without it: {type(e).__name__}: {e}")
        past = []
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

    world.clear()                        # the stacks hold copies
    size = write_squares(QUANT_STEP)
    tail = build_tail(site, work, deps, kit, names, workers) if full else None
    # The Pages budget is the clouds' and the tail's alone: the columns go to
    # Blob only (Erik, 2026-10-02: cloud accuracy first).
    site_bytes = size + _dir_bytes(site / site_tail_tiles_root())
    if site_bytes > SITE_BUDGET_BYTES:
        print(f"::warning::{site_bytes / 1e6:.0f} MB is over the {SITE_BUDGET_BYTES / 1e6:.0f} MB budget: "
              f"rewriting the squares in {QUANT_FALLBACK_STEP} % steps")
        size = write_squares(QUANT_FALLBACK_STEP)
        site_bytes = size + _dir_bytes(site / site_tail_tiles_root())
    print(f"site: {site_bytes / 1e6:.0f} MB in squares and tail")
    n_past = len(past)
    # The day blocks go to Blob only, in QUANT_STEP steps even when Pages had
    # to fall back to 4 %: their size is Blob's to carry, not Pages' budget.
    blob.mkdir(parents=True, exist_ok=True)
    block_bytes = write_blocks(blob, run, names, stacks, hours)
    print(f"blocks: {len(names)} squares, {block_bytes / 1e6:.0f} MB")
    stacks.clear()                       # ~2.5 GB of cloud arrays back before the columns
    past.clear()
    # Pages carries none of the columns, blocks or thickness, so its manifest names none.
    (site / "manifest.json").write_text(site_manifest(run, deps.now(), hours, names, n_past, full, tail, None,
                                                      repairs=repairs))
    thickness = build_thick(blob, state, work, deps, kit, run, steps, names, workers, hours)
    cols = build_columns(blob, state, work, deps, kit, run, steps, names, workers)
    (blob / "manifest.json").write_text(site_manifest(run, deps.now(), hours, names, n_past, full, tail, cols,
                                                      blocks=site_blocks(run), thick=thickness, repairs=repairs))
    print(f"built {len(names)} squares, {len(hours)} steps ({n_past} history)")
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


def site_blocks(run):
    return site.blocks_path(run)


def site_manifest(run, built, hours, names, history_steps, complete, tail=None, columns=None,
                  blocks=None, thick=None, repairs=0):
    return site.build_manifest(run, built, hours, names, history_steps, complete, tail, columns, blocks, thick,
                               repairs)


def write_blocks(blob_dir, run, names, stacks, hours):
    """One .icb (ICL2) per square under blocks/<run>/, cut from the same
    stacks as the squares; returns the bytes written."""
    out = Path(blob_dir) / site_blocks(run)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for name in names:
        lat, lon = parse_name(name)
        cube = np.stack([slice_square(arr, lat, lon) for _, arr in stacks])
        data = blocks.encode_blocks(b"ICL2", lat, lon, hours, quantize(cube, QUANT_STEP))
        (out / f"{name}.icb").write_bytes(data)
        total += len(data)
    return total


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


# ---- Cloud thickness (ICT1, pipeline/blocks.py) -----------------------------
# DWD lists TQI_DIA/TQC_DIA beside the cover fields but they never decide
# whether a run is complete (dwd.THICK_FIELDS): wait this long for them, then
# publish the clouds without thickness rather than hold the feed back.
THICK_WAIT_S = 15 * 60


def site_thick_root():
    return "thick"


def site_thick(run):
    return site.thick_path(run)


def write_thick(blob_dir, run, names, hours, stacks, workers=1):
    """One .ict per square under thick/<run>/; returns the bytes written.
    zlib releases the GIL, so squares encode in parallel threads."""
    out = Path(blob_dir) / site_thick(run)
    out.mkdir(parents=True, exist_ok=True)

    def one(name):
        lat, lon = parse_name(name)
        cube = np.stack([slice_square(arr, lat, lon) for arr in stacks])
        data = blocks.encode_blocks(b"ICT1", lat, lon, hours, cube)
        (out / f"{name}.ict").write_bytes(data)
        return len(data)

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return sum(pool.map(one, names))


def build_thick(blob_dir, state, work, deps, kit, run, steps, names, workers, hours):
    """Cloud ice and water per square (thick.thick_bytes), one .ict under
    thick/<run>/ on Blob, over exactly `hours` — the squares' hours, history
    first — so the app reads both files by one index. An hour no thickness
    covers (before its history began) is all MISSING. Returns the manifest's
    `thick` entry, or None; a failure here never fails the build — the app
    draws the cover without thickness."""
    t0 = time.monotonic()
    shape = (len(dwd.THICK_FIELDS), NY, NX)
    try:
        if not _wait_for_files(deps, run, "thickness", THICK_WAIT_S,
                               lambda: dwd.missing_thick_files(deps.listing, run, steps)):
            return None

        def job(step, field):
            dst = Path(work) / dwd.file_name(run, step, field)
            try:
                deps.download(dwd.file_url(run, step, field), dst)
                # To bytes here, in the worker: a float32 world is 16.6 MB, and
                # 153 steps × 2 fields of them would be 5 GB on a 16 GB runner
                # (bytes: 1.2 GB).
                return step, field, thick.thick_bytes(deps.regrid_values(dst, kit, work))
            finally:
                dst.unlink(missing_ok=True)

        got = {}
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for step, field, b in pool.map(lambda sf: job(*sf), [(s, f) for s in steps for f in dwd.THICK_FIELDS]):
                got[(step, field)] = b
        base = history.hour_of(run)
        own = {base + s: np.stack([got.pop((s, f)) for f in dwd.THICK_FIELDS]) for s in steps}
        past = dict(history.load_history_steps(state, run, sub="thick", shape=shape))
        n_past = sum(1 for h in hours if h in past and h not in own)
        missing = np.full(shape, thick.MISSING, dtype=np.uint8)
        stacks = [own.get(h, past.get(h, missing)) for h in hours]
        head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
        if len(head_steps) == history.HEAD_STEPS:
            history.save_run_head(state, run, np.stack([own[base + s] for s in head_steps]), sub="thick")
            history.prune(state, dwd.run_id(run), sub="thick")
        total = write_thick(blob_dir, run, names, hours, stacks, workers)
    except Exception as e:
        print(f"::warning::thickness failed: {type(e).__name__}: {e}")
        shutil.rmtree(Path(blob_dir) / site_thick_root(), ignore_errors=True)
        return None
    print(f"thickness: {len(names)} squares, {len(hours)} steps ({n_past} history), "
          f"{total / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s")
    return {"format": "ICT1", "run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
            "tiles": site_thick(run)}


# ---- The column feed (docs/column-feed-design.md) ---------------------------
# Every COLUMN_STRIDE-th node of the 0.125° grid: 2 → 0.25°, 21 nodes a side.
# Chosen on measured interpolation error near the ground (design doc, "Node
# spacing"): on steep ground 0.5° nodes miss T 2 m by 3.9 °C at p95 and the
# T80 − T2m inversion signal by 2.1 °C, 0.25° by 2.9 and 1.7. 0.125° itself
# would need ~30 GB of memory for 153 hours on a 16 GB runner.
COLUMN_STRIDE = 2
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
    return keys + [("T_2M", None), ("RELHUM_2M", None), ("T_80M", None), ("WW", None)]


def column_field_names():
    """column_keys as strings ("T@1000", "WW") — stored in each history head,
    so a later build pads a head only when its fields are a prefix of today's."""
    return [f if lv is None else f"{f}@{lv}" for f, lv in column_keys()]


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
    if field == "WW":
        # A WMO 4677 code, whole. Anything outside 0..99 is MISSING, never
        # clipped: 99 is "thunderstorm with heavy hail", and garbage must
        # never decode as severe weather.
        with np.errstate(invalid="ignore"):
            c = np.rint(np.asarray(arr, dtype=np.float64))
            c[~((c >= 0) & (c <= 99))] = np.nan
        return columns.quantize(c, *columns._QUANT[columns.WW])
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


def _wait_for_files(deps, run, what, wait_s, missing_now):
    """Poll DWD's listing every COLUMN_POLL_S until `missing_now()` names no
    file (True) or `wait_s` has passed (False, with a warning)."""
    waited = 0
    while True:
        missing = missing_now()
        if not missing:
            return True
        if waited >= wait_s:
            print(f"::warning::{what}: {len(missing)} files of run {dwd.run_iso(run)} still not listed "
                  f"after {waited // 60} min (first: {missing[0]}) — publishing without {what}")
            return False
        deps.sleep(COLUMN_POLL_S)
        waited += COLUMN_POLL_S


def _wait_for_column_files(deps, run, steps):
    return _wait_for_files(deps, run, "columns", COLUMN_WAIT_S,
                           lambda: dwd.missing_column_files(deps.listing, run, steps))


# Seconds spent downloading and remapping column files, summed over the
# workers; build_columns prints them so a build's log shows where time goes.
_CLOCK = {}
_CLOCK_LOCK = threading.Lock()


def _clock(what, t0):
    with _CLOCK_LOCK:
        _CLOCK[what] = _CLOCK.get(what, 0.0) + time.monotonic() - t0


def _fetch_levels(deps, kit, work, names, run, field, stride, nearest=False):
    """Download DWD files of ONE parameter → {level: lattice array}: one cdo
    decode for all of them, then DWD's weights at the lattice's points
    (`nearest`: the heaviest link, for a categorical field)."""
    paths = []
    try:
        for n in names:
            dst = Path(work) / n
            t0 = time.monotonic()
            deps.download(dwd.dir_url(run, field, n), dst)
            _clock("download", t0)
            paths.append(dst)
        t0 = time.monotonic()
        out = (deps.lattice_levels(paths, kit, work, stride, nearest=True) if nearest
               else deps.lattice_levels(paths, kit, work, stride))
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
        arr = _only(_fetch_levels(deps, kit, work, [dwd.file_name(run, step, group)], run, group, stride,
                                  nearest=group == "WW"))
        return {(group, None): _to_codes((group, None), arr)}
    if group == "T_80M":
        t80 = 0.0
        for lv in dwd.COLUMN_MLEVELS:          # one cdo call each: no level axis to trust
            t = _only(_fetch_levels(deps, kit, work, [dwd.model_file_name(run, step, lv, "T")], run, "T", stride))
            t80 = t80 + w80[lv] * t
        return {("T_80M", None): _to_codes(("T_80M", None), t80)}
    raise KeyError(group)


# The column feed is download-bound: 4,968 of its 6,021 worker-seconds were
# waiting on DWD (2026-10-30), so its fetch runs more threads than the build's
# CPU-bound BUILD_WORKERS. cdo still overlaps; netCDF reads stay serialised
# (regrid._NC_LOCK).
def column_fetch_workers(env=None):
    env = os.environ if env is None else env
    try:
        n = int(env.get("COLUMN_FETCH_WORKERS", "16"))
    except ValueError:
        n = 16
    return max(1, n)


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


def write_columns(blob_dir, run, names, stride, static, stacks, workers=1):
    """One .icc per square under columns/<run>/; returns the bytes written.
    zlib releases the GIL, so squares encode in parallel threads."""
    spec = column_spec()
    hours = [h for h, _ in stacks]
    tiles = Path(blob_dir) / site_columns_tiles(run)
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


def build_columns(blob_dir, state, work, deps, kit, run, steps, names, workers, stride=None):
    """The column feed: per square, every column field at every step (history
    first), one .icc under columns/<run>/. Returns the manifest's `columns`
    entry, or None; a failure here never fails the build — the app keeps its
    Open-Meteo profile."""
    stride = stride or COLUMN_STRIDE
    t0 = time.monotonic()
    try:
        if not _wait_for_column_files(deps, run, steps):
            return None
        static, own = fetch_columns(deps, kit, work, run, steps, column_fetch_workers(), stride)
        # pad: a head saved before a field was appended (WW, 2026-11-07) is
        # still six good hours, the new field missing.
        past = history.load_history_steps(state, run, sub="columns", shape=own[0][1].shape,
                                          pad=columns.MISSING, fields=column_field_names())
        stacks = past + own
        hours = [h for h, _ in stacks]
        base = history.hour_of(run)
        head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
        if len(head_steps) == history.HEAD_STEPS:
            by_hour = dict(own)
            history.save_run_head(state, run, np.stack([by_hour[base + s] for s in head_steps]),
                                  sub="columns", dtype=np.uint16, split=True, fields=column_field_names())
            history.prune(state, dwd.run_id(run), sub="columns")
        total = write_columns(blob_dir, run, names, stride, static, stacks, workers)
    except Exception as e:
        print(f"::warning::columns failed: {type(e).__name__}: {e}")
        shutil.rmtree(Path(blob_dir) / site_columns_root(), ignore_errors=True)
        return None
    print(f"columns: {len(names)} squares, {len(hours)} steps ({len(past)} history), "
          f"{total / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s (worker-seconds: "
          + ", ".join(f"{k} {v:.0f}" for k, v in sorted(_CLOCK.items())) + ")")
    return {"format": "ICC1", "run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
            "stepsMs": [h * 3600 * 1000 for h in hours], "historySteps": len(past),
            "spacing": columns.spacing_deg(stride), "side": columns.side(stride),
            "tiles": site_columns_tiles(run)}


# ---- Upper air and texture clues (ICU1 / ICX1, docs/column-feed-design.md) --
# The cloud-texture feed: T/RH/GH/U/V at dwd.UPPER_PLEVELS plus ICON's four
# convection singles (ICU1, never inside ICC1 — the app's fog reads every
# level of an ICC1 square), and per node the 27 clue bytes of pipeline/clues.py
# (ICX1), computed from the profile ICC1 (1000-700 hPa) + ICU1 (600-200 hPa)
# carry, as their CODES decode — so a phone computing clues from the two files
# gets exactly the bytes ICX1 holds. ICU1 also carries U/V at the ICC1 levels
# (dwd.UPPER_WIND_PLEVELS), so the low deck has wind, shear and shearDir.
#
# Built for a list of squares only (texdev_build.py: the US West). The lattice
# is cropped to the squares' bounding window right after each remap, so 153
# hours of 34 fields stay small; history heads are kept for that window, with
# the window recorded beside them (a different window ignores them).
UPPER_STRIDE = COLUMN_STRIDE
UPPER_WAIT_S = COLUMN_WAIT_S
# GH (m) any real 600-200 hPa surface lies in: 600 hPa sits near 3.5-4.6 km,
# 200 hPa near 10.5-12.7 km. FI left in m² s⁻² (÷ g forgotten) reads ~10x
# higher and fails here instead of being stored, or clipped, as heights.
GH_RANGE_M = (3000.0, 17000.0)
UPPER_SUB = "upper"


def site_upper_root():
    return "upper"


def site_clues_root():
    return "clues"


def upper_keys():
    """The ICU1 fields' keys, in upper_field_spec's (body) order."""
    keys = []
    for p in dwd.UPPER_WIND_PLEVELS:
        keys += [(f, p) for f in dwd.UPPER_WIND_FIELDS]
    for p in dwd.UPPER_PLEVELS:
        keys += [(f, p) for f in dwd.UPPER_FIELDS]
    return keys + [(f, None) for f in dwd.UPPER_SINGLES]


def upper_spec():
    return columns.upper_field_spec(dwd.UPPER_PLEVELS, wind_plevels=dwd.UPPER_WIND_PLEVELS)


_SINGLE_KIND = {"CAPE_CON": columns.CAPE_CON, "CAPE_ML": columns.CAPE_ML,
                "HBAS_CON": columns.HBAS_CON, "HTOP_CON": columns.HTOP_CON}


def check_gh(gh, level):
    """Raise unless every valid GH (m) at `level` hPa lies in GH_RANGE_M."""
    v = np.asarray(gh)[np.isfinite(gh)]
    if v.size and (v.min() < GH_RANGE_M[0] or v.max() > GH_RANGE_M[1]):
        raise ValueError(f"GH at {level} hPa spans {v.min():.0f}-{v.max():.0f} m, outside "
                         f"{GH_RANGE_M[0]:.0f}-{GH_RANGE_M[1]:.0f} m: FI is not geopotential in m2/s2?")


def _upper_to_codes(key, arr):
    """A physical upper-air lattice field (DWD units) → u16 codes, app units.
    FI → metres with _to_codes' g, after the range check."""
    field, lv = key
    if field == "FI":
        check_gh(arr / G, lv)
        return _to_codes(key, arr)
    if field in ("T", "RELHUM"):
        return _to_codes(key, arr)
    kind = {"U": columns.U, "V": columns.V}.get(field) or _SINGLE_KIND[field]
    return columns.quantize(arr, *columns._QUANT[kind])


def square_window(names, stride):
    """(r0, r1, c0, c1): the lattice rows/columns every square's nodes lie in."""
    rows, cols = set(), set()
    for name in names:
        r, c = columns.square_nodes(*parse_name(name), stride)
        rows.update(r)
        cols.update(c)
    return min(rows), max(rows) + 1, min(cols), max(cols) + 1


def _crop(arr, window):
    r0, r1, c0, c1 = window
    return np.ascontiguousarray(arr[..., r0:r1, c0:c1])


def _local_nodes(name, stride, window):
    """The square's node rows and columns as indices into the cropped window."""
    r, c = columns.square_nodes(*parse_name(name), stride)
    return np.asarray(r) - window[0], np.asarray(c) - window[2]


def _window_tag(window, stride):
    return json.dumps({"window": list(window), "stride": stride})


def _windowed_history(state, run, sub, window, stride, shape):
    """history.load_history_steps, but only from heads kept for this window."""
    tag = history.history_dir(state, sub) / "window.json"
    if not tag.exists() or tag.read_text() != _window_tag(window, stride):
        return []
    return history.load_history_steps(state, run, sub=sub, shape=shape)


def _save_windowed_head(state, run, sub, window, stride, head):
    d = history.history_dir(state, sub)
    tag = d / "window.json"
    if tag.exists() and tag.read_text() != _window_tag(window, stride):
        for p in d.glob("*.npz"):            # another window's heads: unreadable now
            p.unlink()
    history.save_run_head(state, run, head, sub=sub, dtype=np.uint16, split=True)
    tag.write_text(_window_tag(window, stride))
    history.prune(state, dwd.run_id(run), sub=sub)


def upper_step(deps, kit, work, run, step, group, stride, window):
    """One step's share of the upper-air fields: {key: cropped u16 codes}."""
    if group in dwd.UPPER_FIELDS:
        want = dwd.upper_levels(group)
        names = [dwd.pressure_file_name(run, step, lv, group) for lv in want]
        levels = _fetch_levels(deps, kit, work, names, run, group, stride)
        if sorted(levels) != sorted(want):
            raise ValueError(f"{group} +{step}: levels {sorted(levels, key=str)}, wanted {want}")
        return {(group, lv): _crop(_upper_to_codes((group, lv), levels[lv]), window) for lv in want}
    if group in dwd.UPPER_SINGLES:
        arr = _only(_fetch_levels(deps, kit, work, [dwd.file_name(run, step, group)], run, group, stride))
        return {(group, None): _crop(_upper_to_codes((group, None), arr), window)}
    raise KeyError(group)


def fetch_upper(deps, kit, work, run, steps, workers, stride, window):
    """[(unix hour, u16 [upper fields][R][C])] in time order, cropped to `window`."""
    jobs = [(s, g) for s in steps for g in list(dwd.UPPER_FIELDS) + list(dwd.UPPER_SINGLES)]
    got = {s: {} for s in steps}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(upper_step, deps, kit, work, run, s, g, stride, window) for s, g in jobs]
        for (s, g), fut in zip(jobs, futs):
            got[s].update(fut.result())
    base = history.hour_of(run)
    keys = upper_keys()
    out = []
    for s in steps:
        fields = got.pop(s)
        out.append((base + s, np.stack([fields[k] for k in keys])))
    return out


def _phys_rows(codes, spec_rows):
    """u16 [fields][N] → float64 app units [fields][N] (NaN = missing), as a
    decoder reads them: f32 scale and offset."""
    sc = np.array([np.float64(np.float32(f[3])) for f in spec_rows])[:, None]
    off = np.array([np.float64(np.float32(f[4])) for f in spec_rows])[:, None]
    c = np.asarray(codes)
    return np.where(c == columns.MISSING, np.nan, off + sc * c.astype(np.float64))


PROFILE_PLEVELS = list(dwd.COLUMN_PLEVELS) + list(dwd.UPPER_PLEVELS)
assert PROFILE_PLEVELS == sorted(PROFILE_PLEVELS, reverse=True)


def profile_rows():
    """Where clue_profile finds each value: rows of column_keys() for the ICC1
    levels' T/RH/GH, rows of upper_keys() for the rest (wind at the ICC1
    levels: None where ICU1 carries none)."""
    ck, uk = column_keys(), upper_keys()
    rows = {"col": {f: [ck.index((f, p)) for p in dwd.COLUMN_PLEVELS] for f in ("T", "RELHUM", "FI")},
            "up": {f: [uk.index((f, p)) for p in dwd.UPPER_PLEVELS] for f in dwd.UPPER_FIELDS},
            "low": {f: [uk.index((f, p)) if (f, p) in uk else None for p in dwd.COLUMN_PLEVELS]
                    for f in ("U", "V")},
            "single": {f: uk.index((f, None)) for f in dwd.UPPER_SINGLES}}
    return rows


def clue_profile(col, up, rows=None):
    """One node's pipeline/clues.py profile from app-unit values: `col` the
    ICC1 dynamic fields (column_keys order; None = no column for this hour),
    `up` the ICU1 fields (upper_keys order)."""
    rows = rows or profile_rows()
    nan = np.full(len(dwd.COLUMN_PLEVELS), np.nan)
    pick = (lambda f: nan) if col is None else (lambda f: np.asarray(col)[rows["col"][f]])
    up = np.asarray(up)
    low = {f: np.array([np.nan if i is None else up[i] for i in rows["low"][f]]) for f in ("U", "V")}
    prof = {"p": PROFILE_PLEVELS,
            "t": np.concatenate([pick("T"), up[rows["up"]["T"]]]),
            "rh": np.concatenate([pick("RELHUM"), up[rows["up"]["RELHUM"]]]),
            "gh": np.concatenate([pick("FI"), up[rows["up"]["FI"]]]),
            "u": np.concatenate([low["U"], up[rows["up"]["U"]]]),
            "v": np.concatenate([low["V"], up[rows["up"]["V"]]])}
    for f, i in rows["single"].items():
        prof[f.lower()] = float(up[i])
    return prof


def node_clue_bytes(col, up, rows=None):
    prof = clue_profile(col, up, rows)
    return clues.encode_clues(clues.deck_clues(prof), clues.node_clues(prof))


def clue_cubes(names, stride, window, stacks, col_by_hour):
    """{square: u8 [hours][side][side][27]} — each node computed once, even
    where squares share an edge."""
    local = {name: _local_nodes(name, stride, window) for name in names}
    nodes = sorted({(r, c) for rr, cc in local.values() for r in rr for c in cc})
    at = {rc: i for i, rc in enumerate(nodes)}
    nr = np.array([r for r, _ in nodes])
    nc = np.array([c for _, c in nodes])
    gather = {name: np.array([[at[(r, c)] for c in cc] for r in rr]) for name, (rr, cc) in local.items()}
    n = columns.side(stride)
    out = {name: np.empty((len(stacks), n, n, clues.N_BYTES), dtype=np.uint8) for name in names}
    col_rows = [f for f in column_spec() if f[1] != columns.STATIC]
    up_rows = upper_spec()
    rows = profile_rows()
    for k, (h, up) in enumerate(stacks):
        upv = _phys_rows(up[:, nr, nc], up_rows)
        col = col_by_hour.get(h)
        colv = None if col is None else _phys_rows(col[:, nr, nc], col_rows)
        b = np.empty((len(nodes), clues.N_BYTES), dtype=np.uint8)
        for i in range(len(nodes)):
            b[i] = np.frombuffer(node_clue_bytes(None if colv is None else colv[:, i], upv[:, i], rows), np.uint8)
        for name in names:
            out[name][k] = b[gather[name]]
    return out


def write_upper(blob_dir, run, names, stride, window, stacks, workers=1):
    """One .icu per square under upper/<run>/; returns the bytes written."""
    spec = upper_spec()
    hours = [h for h, _ in stacks]
    tiles = Path(blob_dir) / site.upper_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    n = columns.side(stride)

    def one(name):
        lat, lon = parse_name(name)
        rr, cc = _local_nodes(name, stride, window)
        cube = np.stack([u[:, rr][:, :, cc] for _, u in stacks])
        data = columns.encode_upper(lat, lon, stride, hours, spec, cube, np.zeros((0, n, n), np.uint16))
        (tiles / f"{name}.icu").write_bytes(data)
        return len(data)

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return sum(pool.map(one, names))


def write_clues(blob_dir, run, names, stride, window, stacks, col_by_hour):
    """One .icx per square under clues/<run>/; returns the bytes written."""
    hours = [h for h, _ in stacks]
    tiles = Path(blob_dir) / site.clues_path(run)
    tiles.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, cube in clue_cubes(names, stride, window, stacks, col_by_hour).items():
        lat, lon = parse_name(name)
        data = cluefile.encode_clue_square(lat, lon, int(round(columns.spacing_deg(stride) * 1000)), hours, cube)
        (tiles / f"{name}.icx").write_bytes(data)
        total += len(data)
    return total


def _tex_entry(fmt, run, hours, n_past, stride, tiles):
    ms = [h * 3600 * 1000 for h in hours]
    # `steps` and `stepsMs` are the same list (unix ms), so a reader written
    # against either name works.
    return {"format": fmt, "run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
            "steps": ms, "stepsMs": ms, "historySteps": n_past,
            "spacing": columns.spacing_deg(stride), "side": columns.side(stride), "tiles": tiles}


def build_upper_and_clues(blob_dir, state, work, deps, kit, run, steps, squares, workers,
                          column_stacks=None, column_sub="columns", stride=None):
    """The upper-air (ICU1) and clue (ICX1) files of `squares`, every step
    with 60 h of upper history first (state/<UPPER_SUB>, history.py), under
    upper/<run>/ and clues/<run>/. The clue history is rebuilt from the upper
    history and the column history. column_stacks: the column build's
    [(hour, u16 [column fields][LY][LX])], history included; None fetches the
    columns here and keeps their head under state/<column_sub>. Returns the
    (upper, clues) manifest entries, or (None, None); never raises."""
    stride = stride or UPPER_STRIDE
    t0 = time.monotonic()
    names = list(squares)
    try:
        window = square_window(names, stride)
        crop_shape = (window[1] - window[0], window[3] - window[2])
        base = history.hour_of(run)
        head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
        whole_head = len(head_steps) == history.HEAD_STEPS

        def missing():
            out = dwd.missing_upper_files(deps.listing, run, steps)
            return out + (dwd.missing_column_files(deps.listing, run, steps) if column_stacks is None else [])

        if not _wait_for_files(deps, run, "upper air", UPPER_WAIT_S, missing):
            return None, None
        if column_stacks is None:
            _, own = fetch_columns(deps, kit, work, run, steps, column_fetch_workers(), stride)
            own = [(h, _crop(c, window)) for h, c in own]
            past = _windowed_history(state, run, column_sub, window, stride, (len(column_keys()),) + crop_shape)
            col_by_hour = dict(past)
            col_by_hour.update(own)
            if whole_head:
                by = dict(own)
                _save_windowed_head(state, run, column_sub, window, stride, np.stack([by[base + s] for s in head_steps]))
        else:
            col_by_hour = {h: _crop(c, window) for h, c in column_stacks}
        own_up = fetch_upper(deps, kit, work, run, steps, column_fetch_workers(), stride, window)
        past_up = _windowed_history(state, run, UPPER_SUB, window, stride, (len(upper_keys()),) + crop_shape)
        if whole_head:
            by = dict(own_up)
            _save_windowed_head(state, run, UPPER_SUB, window, stride, np.stack([by[base + s] for s in head_steps]))
        stacks = past_up + own_up
        hours = [h for h, _ in stacks]
        up_bytes = write_upper(blob_dir, run, names, stride, window, stacks, workers)
        t1 = time.monotonic()
        clue_bytes = write_clues(blob_dir, run, names, stride, window, stacks, col_by_hour)
    except Exception as e:
        print(f"::warning::upper air failed: {type(e).__name__}: {e}")
        for root in (site_upper_root(), site_clues_root()):
            shutil.rmtree(Path(blob_dir) / root, ignore_errors=True)
        return None, None
    print(f"upper air: {len(names)} squares, {len(hours)} steps ({len(past_up)} history), "
          f"ICU1 {up_bytes / 1e6:.1f} MB, ICX1 {clue_bytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s "
          f"(clues {time.monotonic() - t1:.0f} s)")
    return (_tex_entry("ICU1", run, hours, len(past_up), stride, site.upper_path(run)),
            _tex_entry("ICX1", run, hours, len(past_up), stride, site.clues_path(run)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir", default="site")
    ap.add_argument("--blob-dir", default="blob",
                    help="files for Vercel Blob only (day blocks, thickness, the column feed)")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--pages-base", default=None)
    ap.add_argument("--steps", default=None, help="comma list of forecast hours (testing)")
    ap.add_argument("--squares", default=None, help="comma list of square names (testing)")
    a = ap.parse_args()
    steps = [int(s) for s in a.steps.split(",")] if a.steps else None
    squares = a.squares.split(",") if a.squares else None
    sys.exit(build(a.site_dir, a.state_dir, a.work_dir, REAL, a.pages_base, steps, squares, blob=a.blob_dir))


if __name__ == "__main__":
    main()
