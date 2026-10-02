#!/usr/bin/env python3
# tools/measure_columns.py — the column feed on REAL DWD data, measured, not
# published: build time, size per square and for the globe at several node
# spacings, the model levels' heights above ground (for T at 80 m), and one
# decoded column as a sanity check. Run by .github/workflows/columns-measure.yml;
# it writes only under --out and never touches Pages, Blob or state.
import argparse
import os
import sys
import time
import zlib
from pathlib import Path

import netCDF4
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_build                                    # noqa: E402
from pipeline import columns, dwd, history, regrid  # noqa: E402
from pipeline.squares import all_squares            # noqa: E402

HISTORY_REPLAY = 60          # a full build carries 60 history hours; replay steps 0-59 to stand in


def out(line, summary):
    print(line, flush=True)
    if summary:
        with open(summary, "a") as f:
            f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="measure")
    ap.add_argument("--strides", default="2,4,8")
    ap.add_argument("--steps", default=None, help="comma list (default: dwd.STEPS)")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("BUILD_WORKERS", "6")))
    ap.add_argument("--only", choices=["all", "interp"], default="all",
                    help="interp: only the interpolation-error check (for choosing the spacing)")
    a = ap.parse_args()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    strides = sorted(int(s) for s in a.strides.split(","))
    fine = strides[0]
    if any(s % fine for s in strides):
        raise SystemExit("every stride must be a multiple of the finest")
    steps = [int(s) for s in a.steps.split(",")] if a.steps else dwd.STEPS
    deps = run_build.REAL
    work = Path(a.out) / "work"
    work.mkdir(parents=True, exist_ok=True)

    run = dwd.newest_complete_run(deps.listing)
    out(f"## Column feed measurement — run {dwd.run_iso(run)}", summary)
    missing = dwd.missing_column_files(deps.listing, run, steps)
    out(f"column files missing from DWD's listing: {len(missing)} {missing[:3]}", summary)
    if missing:
        raise SystemExit(1)
    kit = deps.ensure_kit(work)
    if a.only == "interp":
        interpolation_error(deps, kit, work, run, strides, summary)
        return

    # DWD's weights, and our lattice cut of them checked against cdo's own
    # full remap on real fields (step 0: T on seven levels, T_2M, HSURF).
    t0 = time.monotonic()
    remap = regrid.lattice_remap(kit, fine)
    with netCDF4.Dataset(Path(kit) / regrid.WEIGHTS_FILE) as w:
        dims = {k: len(v) for k, v in w.dimensions.items()}
        method = getattr(w, "map_method", "?")
    out(f"weights: {dims}, map_method {method!r}; lattice cut at stride {fine}: {remap.node.size} links, "
        f"{int(remap.empty.sum())} points without one, {time.monotonic() - t0:.0f} s", summary)
    checks = [("T", [dwd.pressure_file_name(run, 0, lv, "T") for lv in dwd.COLUMN_PLEVELS]),
              ("T_2M", [dwd.file_name(run, 0, "T_2M")]),
              ("HSURF", [dwd.invariant_file_name(run, "HSURF")])]
    out("| check | levels | cdo full remap s | lattice s | max abs diff | NaN cdo / ours |\n|---|---|---|---|---|---|", summary)
    for field, names in checks:
        paths = []
        for n in names:
            dst = work / n
            deps.download(dwd.dir_url(run, field, n), dst)
            paths.append(dst)
        t0 = time.monotonic()
        full = regrid.regrid_levels(paths, kit, work)
        t_full = time.monotonic() - t0
        t0 = time.monotonic()
        ours = regrid.lattice_levels(paths, kit, work, fine)
        t_ours = time.monotonic() - t0
        if sorted(full, key=str) != sorted(ours, key=str):
            raise SystemExit(f"{field}: levels differ {sorted(full, key=str)} vs {sorted(ours, key=str)}")
        diff = max(float(np.nanmax(np.abs(columns.lattice(full[k], fine) - ours[k]))) for k in full)
        nan = (sum(int(np.isnan(columns.lattice(full[k], fine)).sum()) for k in full),
               sum(int(np.isnan(ours[k]).sum()) for k in ours))
        out(f"| {field} | {len(full)} | {t_full:.1f} | {t_ours:.1f} | {diff:.2e} | {nan[0]} / {nan[1]} |", summary)
        for p in paths:
            p.unlink(missing_ok=True)
        del full, ours

    # The lowest model levels' heights above ground, from HHL (at the lattice).
    t0 = time.monotonic()
    hs = run_build._only(run_build._fetch_levels(deps, kit, work, [dwd.invariant_file_name(run, "HSURF")], run, "HSURF", fine))
    hhl = {}
    for lv in range(114, 122):
        hhl[lv] = run_build._only(run_build._fetch_levels(deps, kit, work, [dwd.invariant_file_name(run, "HHL", lv)], run, "HHL", fine))
    out(f"HHL[121] − HSURF: max |Δ| {np.nanmax(np.abs(hhl[121] - hs)):.2f} m", summary)
    out(f"model levels' height above ground, every node of the {columns.spacing_deg(fine)}° lattice:", summary)
    out("| full level | AGL p1 | p50 | p99 | min | max |\n|---|---|---|---|---|---|", summary)
    for lv in range(114, 121):
        z = (hhl[lv] + hhl[lv + 1]) / 2 - hhl[121]
        p = np.nanpercentile(z, [1, 50, 99])
        out(f"| {lv} | {p[0]:.1f} | {p[1]:.1f} | {p[2]:.1f} | {np.nanmin(z):.1f} | {np.nanmax(z):.1f} |", summary)
    w = run_build.t80_weights({lv: hhl[lv] for lv in dwd.COLUMN_HHL_LEVELS}, hs)
    top, bottom = min(dwd.COLUMN_MLEVELS), max(dwd.COLUMN_MLEVELS)
    n = np.isfinite(w[top]).sum()
    z_top = (hhl[top] + hhl[top + 1]) / 2 - hhl[121]
    out(f"T80 pairs over {n} nodes: {(w[top] > 0).sum() / n:.2%} use level {top} (80 m above level "
        f"{bottom - 1}), {(z_top < 80).sum() / n:.3%} have every level below 80 m (clamped to {top}), "
        f"{(~np.isfinite(w[top])).sum()} missing", summary)
    del hs, hhl, w
    out(f"statics probe: {time.monotonic() - t0:.0f} s", summary)

    # The build's own fetch, at the finest stride, timed.
    t0 = time.monotonic()
    static, own = run_build.fetch_columns(deps, kit, work, run, steps, a.workers, fine)
    fetch_s = time.monotonic() - t0
    out(f"fetch (download + cdo + quantise), {len(steps)} steps, {a.workers} workers: "
        f"**{fetch_s / 60:.1f} min**; worker-seconds: "
        + ", ".join(f"{k} {v:.0f}" for k, v in sorted(run_build._CLOCK.items())), summary)

    # 60 replayed history hours + the run's own steps = a full build's 153.
    base = history.hour_of(run)
    replay = [(base - HISTORY_REPLAY + i, own[i][1]) for i in range(min(HISTORY_REPLAY, len(own)))]
    names = all_squares()
    out("| spacing | side | steps | globe MB | per square KB min / median / max | encode s | state head MB |\n"
        "|---|---|---|---|---|---|---|", summary)
    for stride in strides:
        k = stride // fine
        st = static[:, ::k, ::k]
        for label, stacks in (("run only", own), ("+60 h history", replay + own)):
            t0 = time.monotonic()
            site = Path(a.out) / f"site_{stride}_{len(stacks)}"
            total = run_build.write_columns(site, run, names, stride, st,
                                            [(h, c[:, ::k, ::k]) for h, c in stacks], a.workers)
            enc_s = time.monotonic() - t0
            sizes = sorted(p.stat().st_size for p in (site / run_build.site_columns_tiles(run)).glob("*.icc"))
            head = Path(a.out) / f"head_{stride}"
            history.save_run_head(head, run, np.stack([c[:, ::k, ::k] for _, c in own[:6]]),
                                  sub="columns", dtype=np.uint16)
            head_mb = history.run_file(head, run, "columns").stat().st_size / 1e6
            out(f"| {columns.spacing_deg(stride)}° | {columns.side(stride)} | {len(stacks)} ({label}) | "
                f"{total / 1e6:.1f} | {sizes[0] / 1e3:.1f} / {sizes[len(sizes) // 2] / 1e3:.1f} / "
                f"{sizes[-1] / 1e3:.1f} | {enc_s:.0f} | {head_mb:.1f} |", summary)

    # Where a square's bytes go: each kind's codes compressed on their own
    # (N40W125, run + history), so the size is not a mystery.
    for stride in strides:
        site = Path(a.out) / f"site_{stride}_{len(replay) + len(own)}"
        d = columns.decode_columns((site / run_build.site_columns_tiles(run) / "N40W125.icc").read_bytes())
        parts = {}
        for f, codes in zip(d["spec"], d["fields"]):
            key = f"{columns.KIND_NAMES[f[0]]}{'' if f[1] == columns.PRESSURE_HPA else f'@{f[2]}'}"
            parts.setdefault(key, []).append(codes.reshape(-1))
        sizes = {k: len(zlib.compress(columns._planes(np.concatenate(v)), columns.ZLIB_LEVEL))
                 for k, v in parts.items()}
        out(f"N40W125 at {columns.spacing_deg(stride)}°, bytes by field: "
            + ", ".join(f"{k} {v / 1e3:.1f} KB" for k, v in sizes.items()), summary)

    # One decoded column: Bend, OR (44.06 N, 121.31 W), nearest node at the
    # coarsest stride, first step — units must read as the app's.
    stride = strides[-1]
    site = Path(a.out) / f"site_{stride}_{len(own)}"
    d = columns.decode_columns((site / run_build.site_columns_tiles(run) / "N40W125.icc").read_bytes())
    sp = d["spacing_mdeg"] / 1000
    r, c = round((44.06 - 40) / sp), round((-121.31 + 125) / sp)
    out(f"Bend node ({40 + r * sp:.2f}, {-125 + c * sp:.2f}), hour {d['step_hours'][0]}:", summary)
    for f, codes in zip(d["spec"], d["fields"]):
        v = columns.dequantize(codes[r, c] if f[1] == columns.STATIC else codes[r, c, 0], f[3], f[4])
        out(f"  {columns.KIND_NAMES[f[0]]:5s} lt{f[1]} {f[2]:5d}: {float(v):9.2f}", summary)


# ---- What a coarser spacing costs where it matters: near the ground ----------

INTERP_STEPS = list(range(0, 24, 3))          # one day, every 3 h: night inversions and afternoon mixing
BEND = (44.06, -121.31)


def bilinear_from(coarse, m, shape):
    """Every m-th point (coarse) → bilinear back onto the full [ny][nx] grid."""
    ny, nx = shape
    cy, cx = coarse.shape

    def axis(n, c):
        x = np.arange(n) / m
        lo = np.minimum(np.floor(x).astype(int), c - 1)
        hi = np.minimum(lo + 1, c - 1)
        return lo, hi, np.clip(x - lo, 0.0, 1.0)
    i0, i1, fi = axis(ny, cy)
    j0, j1, fj = axis(nx, cx)
    top = coarse[i0][:, j0] * (1 - fj) + coarse[i0][:, j1] * fj
    bot = coarse[i1][:, j0] * (1 - fj) + coarse[i1][:, j1] * fj
    return top * (1 - fi)[:, None] + bot * fi[:, None]


def interpolation_error(deps, kit, work, run, strides, summary):
    """The full 0.125° grid (what Open-Meteo serves a point from) is the
    truth; each spacing's nodes, bilinearly interpolated to every 0.125°
    point between them, the estimate. Fields: model surface height, T and
    RH at 2 m, T at 80 m, and the near-surface inversion T80 − T2m (> 0:
    warmer aloft, the stable layer radiation fog forms under), over one day."""
    def one(field, name):
        return run_build._only(run_build._fetch_levels(deps, kit, work, [name], run, field, 1)).astype(np.float64)
    hs = one("HSURF", dwd.invariant_file_name(run, "HSURF"))
    hhl = {lv: one("HHL", dwd.invariant_file_name(run, "HHL", lv)) for lv in dwd.COLUMN_HHL_LEVELS}
    w80 = run_build.t80_weights(hhl, hs)
    # Steep ground: more than 300 m of relief among a point and its 8 neighbours.
    pad = np.pad(hs, 1, mode="edge")
    win = np.stack([pad[1 + di:1 + di + hs.shape[0], 1 + dj:1 + dj + hs.shape[1]]
                    for di in (-1, 0, 1) for dj in (-1, 0, 1)])
    steep = (win.max(0) - win.min(0)) > 300.0
    land = hs > 1.0
    out(f"### Interpolation error by spacing (truth: the 0.125° grid; {land.sum()} land points, "
        f"{steep.sum()} on steep ground, {len(INTERP_STEPS)} hours of run {dwd.run_iso(run)})", summary)
    fields = {"HSURF m": [hs]}
    for key in ("T 2 m °C", "T 80 m °C", "T80 − T2m °C", "RH 2 m %"):
        fields[key] = []
    for s in INTERP_STEPS:
        t2 = one("T_2M", dwd.file_name(run, s, "T_2M")) - run_build.K0
        rh = one("RELHUM_2M", dwd.file_name(run, s, "RELHUM_2M"))
        t80 = sum(w80[lv] * one("T", dwd.model_file_name(run, s, lv, "T")) for lv in dwd.COLUMN_MLEVELS) - run_build.K0
        fields["T 2 m °C"].append(t2)
        fields["T 80 m °C"].append(t80)
        fields["T80 − T2m °C"].append(t80 - t2)
        fields["RH 2 m %"].append(rh)
    rows = []
    bi, bj = round((BEND[0] + 90) / 0.125), round((BEND[1] + 180) / 0.125)
    bend = {}
    for stride in strides:
        off = np.ones(hs.shape, dtype=bool)
        off[::stride, ::stride] = False              # points the nodes do not already hold
        for key, arrs in fields.items():
            errs = {"land": [], "steep": []}
            for k, truth in enumerate(arrs):
                est = bilinear_from(truth[::stride, ::stride], stride, truth.shape)
                e = np.abs(est - truth)
                errs["land"].append(e[off & land])
                errs["steep"].append(e[off & steep])
                bend.setdefault((stride, key), []).append((float(truth[bi, bj]), float(est[bi, bj])))
            cells = []
            for sub in ("land", "steep"):
                e = np.concatenate(errs[sub])
                e = e[np.isfinite(e)]
                p = np.percentile(e, [50, 95, 99]) if e.size else [np.nan] * 3
                cells.append(f"{p[0]:.2f} / {p[1]:.2f} / {p[2]:.2f}")
            rows.append(f"| {columns.spacing_deg(stride)}° | {key} | {cells[0]} | {cells[1]} |")
    out("| spacing | field | land: abs error p50 / p95 / p99 | steep ground: p50 / p95 / p99 |\n"
        "|---|---|---|---|", summary)
    for r in rows:
        out(r, summary)
    out(f"Bend, OR ({bi * 0.125 - 90:.3f}, {bj * 0.125 - 180:.3f}), truth → estimate per spacing, "
        f"hours {INTERP_STEPS}:", summary)
    for (stride, key), pairs in bend.items():
        out(f"  {columns.spacing_deg(stride)}° {key}: " + ", ".join(f"{t:.1f}→{e:.1f}" for t, e in pairs[:8]), summary)


if __name__ == "__main__":
    main()
