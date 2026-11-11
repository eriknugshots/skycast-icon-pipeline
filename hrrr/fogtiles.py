# hrrr/fogtiles.py — HRRR's fog fields as ICC1 tiles, 41 x 41 nodes of 25 mdeg
# (about 2 km) over 1-degree tiles, a block of FOG_BLOCK_HOURS steps per file:
#   fog/<YYYYMMDDHH>/<tile>/<k>.icc, block k = steps [k*B, (k+1)*B) of the run.
# Nodes are bilinearly sampled from HRRR's 3 km Lambert grid (hrrr/fog.py). The
# lattice-to-grid indices are computed once per run and reused for every field
# and hour. Constants: docs/hrrr-fog-measure.md.
from pathlib import Path

import numpy as np

from pipeline.columns import encode_columns_grid, field_spec_hrrr, quantize, STATIC
from pipeline.dwd import run_id, run_iso

from .fog import FOG_FIELDS, read_fog_fields, lcc_params, lcc_ij, sample_bilinear

FOG_SPACING_MDEG = 25
FOG_TILE_DEG = 1
FOG_BLOCK_HOURS = 6
FOG_N = 41
LAT_RANGE = (21, 52)      # sw corners, inclusive
LON_RANGE = (-135, -60)


def tile_name(sw_lat, sw_lon):
    return f"{'N' if sw_lat >= 0 else 'S'}{abs(sw_lat):02d}{'E' if sw_lon >= 0 else 'W'}{abs(sw_lon):03d}"


def tile_nodes(sw_lat, sw_lon):
    """(lat2d, lon2d), each [41][41]: row 0 the south edge, column 0 the west."""
    step = FOG_SPACING_MDEG / 1000.0
    lat = sw_lat + step * np.arange(FOG_N)
    lon = sw_lon + step * np.arange(FOG_N)
    return np.meshgrid(lat, lon, indexing="ij")


def fog_path(run):
    return f"fog/{run_id(run)}"


def _sample(fields, key, ny, nx, fi, fj):
    return sample_bilinear(fields[key].reshape(ny, nx), fi, fj)


def build_fog(run, out_dir, get, steps):
    """Write out_dir/fog/<YYYYMMDDHH>/<tile>/<k>.icc and return the manifest's
    `fog` entry. steps: the forecast hours of the run (a list) or their count
    (hours 0..steps-1). Memory is one block of tiles at a time."""
    fhs = list(range(steps)) if isinstance(steps, int) else [int(f) for f in steps]
    if not fhs or any(b <= a for a, b in zip(fhs, fhs[1:])):
        raise ValueError(f"steps must be non-empty and strictly ascending, got {steps!r}")
    out_dir = Path(out_dir)
    base_hour = int(run.timestamp() // 3600)

    # Candidate lattice: every tile in range, regridded once.
    cands = [(la, lo) for la in range(LAT_RANGE[0], LAT_RANGE[1] + 1)
             for lo in range(LON_RANGE[0], LON_RANGE[1] + 1)]
    lat = np.stack([tile_nodes(la, lo)[0] for la, lo in cands])
    lon = np.stack([tile_nodes(la, lo)[1] for la, lo in cands])

    first = read_fog_fields(run, fhs[0], get)
    ny, nx = first["Ny"], first["Nx"]
    p = lcc_params(first["grid"])
    fi, fj = lcc_ij(lat, lon, p)
    del lat, lon

    # A tile is published only when the step-0 surface height and 2 m T are
    # defined at all of its nodes (HRRR's domain interior).
    ok = np.ones(len(cands), bool)
    for key in (("HGT", "surface"), ("TMP", "2 m above ground")):
        ok &= np.isfinite(_sample(first, key, ny, nx, fi, fj)).all(axis=(1, 2))
    keep = np.flatnonzero(ok)
    names = [tile_name(*cands[k]) for k in keep]
    fi, fj = fi[keep], fj[keep]

    spec = field_spec_hrrr()
    dyn_spec = [f for f in spec if f[1] != STATIC]
    dyn_keys = [k for k in FOG_FIELDS if k != ("HGT", "surface")]
    assert len(dyn_keys) == len(dyn_spec)
    sta_spec = [f for f in spec if f[1] == STATIC]

    def codes_for(fields):
        return np.stack([quantize(_sample(fields, key, ny, nx, fi, fj), f[3], f[4])
                         for key, f in zip(dyn_keys, dyn_spec)], axis=1)   # [tiles][fields][n][n]

    static = quantize(_sample(first, ("HGT", "surface"), ny, nx, fi, fj),
                      sta_spec[0][3], sta_spec[0][4])[:, None]             # [tiles][1][n][n]
    codes0 = codes_for(first)
    del first   # the raw step-0 fields are 27 x 1.9 M floats; keep only the codes

    root = out_dir / fog_path(run)
    for k0 in range(0, len(fhs), FOG_BLOCK_HOURS):
        block = fhs[k0:k0 + FOG_BLOCK_HOURS]
        cube = np.empty((len(names), len(block)) + codes0.shape[1:], np.uint16)   # [tiles][steps][fields][n][n]
        for s_i, fh in enumerate(block):
            cube[:, s_i] = codes0 if fh == fhs[0] else codes_for(read_fog_fields(run, fh, get))
        hours = [base_hour + fh for fh in block]
        k = k0 // FOG_BLOCK_HOURS
        for t, name in enumerate(names):
            la, lo = cands[keep[t]]
            d = root / name
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{k}.icc").write_bytes(encode_columns_grid(
                la, lo, FOG_SPACING_MDEG, FOG_N, hours, spec, cube[t], static[t]))
    return {"format": "ICC1", "run": run_iso(run), "runMs": int(run.timestamp() * 1000),
            "stepsMs": [(base_hour + fh) * 3600_000 for fh in fhs], "tiles": fog_path(run),
            "tileDeg": FOG_TILE_DEG, "spacingMdeg": FOG_SPACING_MDEG, "blockHours": FOG_BLOCK_HOURS,
            "tileNames": names}
