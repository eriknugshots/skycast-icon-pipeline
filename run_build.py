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
import subprocess
import sys
import urllib.request
from collections import namedtuple
from pathlib import Path
import numpy as np

from pipeline import dwd, history, live, site
from pipeline.encode import encode_square, quantize, FIELD_COUNT
from pipeline.regrid import ensure_kit, regrid
from pipeline.squares import all_squares, parse_name, slice_square, NX, NY

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1
ROOT = Path(__file__).resolve().parent

Deps = namedtuple("Deps", "listing fetch_text download regrid ensure_kit now")


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
            now=lambda: dt.datetime.now(dt.timezone.utc))


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

    size = write_squares(QUANT_STEP)
    tail = build_tail(site, work, deps, kit, names, workers) if full else None
    site_bytes = size + _dir_bytes(site / site_tail_tiles_root())
    if site_bytes > SITE_BUDGET_BYTES:
        print(f"::warning::{site_bytes / 1e6:.0f} MB is over the {SITE_BUDGET_BYTES / 1e6:.0f} MB budget: "
              f"rewriting the squares in {QUANT_FALLBACK_STEP} % steps")
        size = write_squares(QUANT_FALLBACK_STEP)
        site_bytes = size + _dir_bytes(site / site_tail_tiles_root())
    print(f"site: {site_bytes / 1e6:.0f} MB in squares")
    (site / "manifest.json").write_text(site_manifest(run, deps.now(), hours, names, len(past), full, tail))

    # The run's hours 0-5, for the next build's history — only when they were built.
    head_steps = [s for s in range(history.HEAD_STEPS) if s in steps]
    if len(head_steps) == history.HEAD_STEPS:
        head = np.stack([np.stack([world[(s, f)] for f in dwd.FIELDS]) for s in head_steps])
        history.save_run_head(state, run, head)
        history.prune(state, dwd.run_id(run))
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


def site_manifest(run, built, hours, names, history_steps, complete, tail=None):
    return site.build_manifest(run, built, hours, names, history_steps, complete, tail)


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
