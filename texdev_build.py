#!/usr/bin/env python3
# texdev_build.py — the cloud-texture dev feed: upper air (ICU1) and texture
# clues (ICX1) of one ICON run, for the United States and Iceland, under texdev/.
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

# The United States and Iceland (Erik, 2026-11-13: "lets start with just the US
# and iceland for now, and then build the whole world later"), as boxes of 5°
# squares by their SW corners, each with about one square of sea or border
# around the land: a view from the coast looks hundreds of km out to sea, and
# the app draws no texture where a square is missing. The Aleutians west of
# 180° are left out: one square past the dateline would stretch the build's
# single window (run_build.square_window) round the whole globe.
#   (lat from, lat to, lon from, lon to), SW corners, inclusive
TEXDEV_BOXES = (
    (15, 50, -130, -65),    # the lower 48 states
    (50, 70, -180, -130),   # Alaska
    (10, 25, -170, -150),   # Hawaii
    (55, 70, -30, -10),     # Iceland
)
TEXDEV_SQUARES = sorted({square_name(lat, lon)
                         for la0, la1, lo0, lo1 in TEXDEV_BOXES
                         for lat in range(la0, la1 + 5, 5)
                         for lon in range(lo0, lo1 + 5, 5)})
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
