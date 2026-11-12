#!/usr/bin/env python3
# texdev_build.py — the cloud-texture dev feed: upper air (ICU1) and texture
# clues (ICX1) of one ICON run, for the US West squares only, under texdev/.
#
# It takes the run a normal build would (the newest whole run on DWD), waits
# for its column and upper-air files the same way, and writes
#   <out>/texdev/upper/<run>/<square>.icu
#   <out>/texdev/clues/<run>/<square>.icx
#   <out>/texdev/manifest.json   {run, runMs, builtMs, squares, upper, clues}
# and nothing else: no cloud squares, no ICC1 columns, no live path. Its
# history lives in its own state dir: state/upper (run_build.UPPER_SUB) and
# state/texdev_columns (the column heads the clue history is rebuilt from).
import argparse
import json
import sys
from pathlib import Path

import run_build
from pipeline import dwd
from pipeline.squares import square_name

# The 5° squares with SW corner lat 35/40/45 and lon -130/-125/-120/-115:
# 35-50 °N, 130-110 °W — every cloud-texture test case is in Oregon or
# Washington.
TEXDEV_LATS = (35, 40, 45)
TEXDEV_LONS = (-130, -125, -120, -115)
TEXDEV_SQUARES = [square_name(lat, lon) for lat in TEXDEV_LATS for lon in TEXDEV_LONS]
PREFIX = "texdev"
COLUMN_SUB = "texdev_columns"


def manifest(run, built, names, upper, clues):
    return json.dumps({"run": dwd.run_iso(run), "runMs": int(run.timestamp() * 1000),
                       "builtMs": int(built.timestamp() * 1000), "squares": sorted(names),
                       "upper": upper, "clues": clues, "attribution": run_build.site.ATTRIBUTION},
                      separators=(",", ":"))


def build(out, state, work, deps, steps=None, workers=None, squares=None):
    run = dwd.newest_complete_run(deps.listing)
    if run is None:
        print("no complete run on DWD yet")
        return run_build.NOTHING_TO_DO
    steps = list(steps) if steps is not None else dwd.STEPS
    names = list(squares) if squares is not None else list(TEXDEV_SQUARES)
    root, work = Path(out) / PREFIX, Path(work)
    work.mkdir(parents=True, exist_ok=True)
    kit = deps.ensure_kit(work)
    workers = workers or 6
    print(f"texdev: run {dwd.run_iso(run)}, {len(steps)} steps, {len(names)} squares")
    upper, clues = run_build.build_upper_and_clues(root, Path(state), work, deps, kit, run, steps, names, workers,
                                                   column_sub=COLUMN_SUB)
    if upper is None or clues is None:
        print(f"::error::texdev: run {dwd.run_iso(run)} built no upper air — nothing published")
        return run_build.FAILED
    (root / "manifest.json").write_text(manifest(run, deps.now(), names, upper, clues))
    return run_build.BUILT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="blob", help="texdev/ is written under it")
    ap.add_argument("--state-dir", default="state-texdev")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--steps", default=None, help="comma list of forecast hours (testing)")
    a = ap.parse_args()
    steps = [int(s) for s in a.steps.split(",")] if a.steps else None
    sys.exit(build(a.out_dir, a.state_dir, a.work_dir, run_build.REAL, steps))


if __name__ == "__main__":
    main()
