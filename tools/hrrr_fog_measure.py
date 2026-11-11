# tools/hrrr_fog_measure.py — size the HRRR fog files before building them.
# Reads f00..f(N-1) of wrfprsf by idx + byte range, samples nearest-cell onto a
# 25 mdeg lattice for four 1-degree tiles round Bend, encodes each with the ICC1
# byte layout (local copy of the encoder, 41x41 nodes), and prints bytes per
# tile at the measured step count plus a per-hour figure for 6/12/24 h blocks.
# Usage: python tools/hrrr_fog_measure.py [steps=4] [cache_dir]
import datetime as dt, os, struct, sys, urllib.request, zlib
import numpy as np
import eccodes
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.columns import (T, RH, GH, HSURF, STATIC, PRESSURE_HPA, HEIGHT_AGL_M, _QUANT,
                              quantize, _planes, MAGIC, _HEADER, _FIELD, ZLIB_LEVEL)

HOST = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700)
SPACING_MDEG, TILE_DEG = 25, 1
TILES = [(43, -122), (44, -122), (43, -121), (44, -121)]
N = TILE_DEG * 1000 // SPACING_MDEG + 1   # 41


def url(run, f):
    return f"{HOST}/hrrr.{run:%Y%m%d}/conus/hrrr.t{run.hour:02d}z.wrfprsf{f:02d}.grib2"


def get(u, rng=None):
    """Range read that resumes from the bytes already received when the link
    drops, and only returns once the byte count matches what the server said."""
    start = rng[0] if rng else 0
    end = rng[1] if rng else None
    buf = b""
    want = None if end is None else end - start + 1
    for attempt in range(60):
        req = urllib.request.Request(u)
        if rng or buf:
            req.add_header("Range", f"bytes={start + len(buf)}-{'' if end is None else end}")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if want is None and r.headers.get("Content-Length"):
                    want = int(r.headers["Content-Length"]) + len(buf)
                while True:
                    c = r.read(65536)
                    if not c:
                        break
                    buf += c
            if want is not None and len(buf) == want:
                return buf
        except Exception:
            if attempt == 59:
                raise
    raise IOError(f"short read {len(buf)} of {want} for {u}")


def newest_run():
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    r = now - dt.timedelta(hours=now.hour % 6)
    for _ in range(8):
        try:
            urllib.request.urlopen(urllib.request.Request(url(r, 48) + ".idx", method="HEAD"), timeout=30)
            return r
        except Exception:
            r -= dt.timedelta(hours=6)
    raise SystemExit("no complete run found")


def wanted(f):
    """[(key, var, level text)] for one step, in dynamic-field order."""
    out = []
    for p in LEVELS:
        for k, v in ((T, "TMP"), (RH, "RH"), (GH, "HGT")):
            out.append(((k, PRESSURE_HPA, p), v, f"{p} mb"))
    out.append(((T, HEIGHT_AGL_M, 2), "TMP", "2 m above ground"))
    out.append(((RH, HEIGHT_AGL_M, 2), "RH", "2 m above ground"))
    return out


def ranges(idx, f, items, extra=()):
    rows = [ln.split(":") for ln in idx.strip().split("\n") if ln.strip()]
    tm = "anl" if f == 0 else f"{f} hour fcst"
    res = {}
    for key, v, lv in list(items) + list(extra):
        k = next(i for i, r in enumerate(rows) if len(r) > 5 and r[3] == v and r[4] == lv and r[5] == tm)
        res[(key, v, lv)] = (int(rows[k][1]), int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None)
    return res


def decode(raw, geo=None):
    g = eccodes.codes_new_from_message(raw)
    try:
        vals = eccodes.codes_get_values(g)
        miss = eccodes.codes_get(g, "missingValue")
        out = np.where(np.isclose(vals, miss), np.nan, vals)
        if geo is not None:
            geo["lat"] = eccodes.codes_get_array(g, "latitudes")
            geo["lon"] = eccodes.codes_get_array(g, "longitudes")
            geo["keys"] = {k: eccodes.codes_get(g, k) for k in (
                "Latin1InDegrees", "Latin2InDegrees", "LoVInDegrees", "latitudeOfFirstGridPointInDegrees",
                "longitudeOfFirstGridPointInDegrees", "DxInMetres", "DyInMetres", "Nx", "Ny")}
    finally:
        eccodes.codes_release(g)
    return out


def encode(sw_lat, sw_lon, hours, spec, dyn, stat):
    """ICC1 bytes for an NxN tile; dyn [steps][dynfields][N][N] u16, stat [static][N][N]."""
    parts, di, si = [], 0, 0
    for f in spec:
        if f[1] == STATIC:
            parts.append(stat[si].reshape(-1)); si += 1
        else:
            parts.append(np.moveaxis(dyn[:, di], 0, -1).reshape(-1)); di += 1
    body = np.concatenate(parts)
    head = _HEADER.pack(MAGIC, sw_lat, sw_lon, SPACING_MDEG, N, N, len(hours), len(spec))
    return (head + struct.pack(f"<{len(hours)}I", *hours)
            + b"".join(_FIELD.pack(*f) for f in spec) + zlib.compress(_planes(body), ZLIB_LEVEL))


def main():
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    run = newest_run()
    print("run", run.isoformat(), "steps", steps)
    geo = {}
    cache = {}
    sel = {}   # tile -> (flat cell index per node) built once geo is known
    dyn = {t: [] for t in TILES}
    stat = {}
    nbytes = 0
    for f in range(steps):
        u = url(run, f)
        idx = get(u + ".idx").decode()
        items = wanted(f)
        extra = [(("hsurf",), "HGT", "surface")] if f == 0 else []
        rg = ranges(idx, f, items, extra)
        planes = {}
        for (key, v, lv), rng in rg.items():
            raw = get(u, rng); nbytes += len(raw)
            planes[key] = decode(raw, geo if not geo else None)
            if not sel:
                lat, lon = geo["lat"], geo["lon"]
                lon = np.where(lon > 180, lon - 360, lon)
                geo["lon180"] = lon
                near = (lat > 42.8) & (lat < 45.2) & (lon > -122.2) & (lon < -120.8)
                cells = np.flatnonzero(near)
                for (la, lo) in TILES:
                    nl = la + np.arange(N) * SPACING_MDEG / 1000.0
                    no = lo + np.arange(N) * SPACING_MDEG / 1000.0
                    LA, LO = np.meshgrid(nl, no, indexing="ij")
                    cl, co = lat[cells], lon[cells]
                    d = ((LA.reshape(-1, 1) - cl) ** 2 + ((LO.reshape(-1, 1) - co) * np.cos(np.radians(LA.reshape(-1, 1)))) ** 2)
                    sel[(la, lo)] = cells[np.argmin(d, axis=1)].reshape(N, N)
                print(f"cells in box: {cells.size}")
        for t in TILES:
            cols = []
            for key, v, lv in items:
                a = planes[key][sel[t]]
                a = a - 273.15 if key[0] == T else a
                cols.append(quantize(a, *_QUANT[key[0]]))
            dyn[t].append(np.stack(cols))
            if f == 0:
                stat[t] = quantize(planes[("hsurf",)][sel[t]], *_QUANT[HSURF])[None]
        print(f"f{f:02d} read, {nbytes/1e6:.1f} MB downloaded so far", flush=True)
    spec = [k + _QUANT[k[0]] for k, _, _ in wanted(0)] + [(HSURF, STATIC, 0) + _QUANT[HSURF]]
    spec = [(k[0], k[1], k[2], *_QUANT[k[0]]) if len(k) == 3 else k for k in [(a[0], a[1], a[2]) + tuple(a[3:]) for a in spec]]
    base = int(run.timestamp() // 3600)
    hours = [base + f for f in range(steps)]
    sizes = {}
    for t in TILES:
        blob = encode(t[0], t[1], hours, spec, np.stack(dyn[t]).astype(np.uint16), stat[t].astype(np.uint16))
        sizes[t] = len(blob)
        print(f"tile N{t[0]}W{-t[1]}: {len(blob)} bytes for {steps} steps, {len(spec)} fields")
    # Cost model: file bytes = header + steps*(per-step) ; fit from two points would need
    # more steps, so scale body linearly per hour (header is ~0.5 kB, negligible).
    mean = sum(sizes.values()) / len(sizes)
    per_hour = mean / steps
    print(f"mean per tile {mean:.0f} B, {per_hour:.0f} B per hour")
    for B in (6, 12, 24):
        tile_b = per_hour * B
        blocks = -(-24 // B) + 1
        print(f"B={B:2d}h: {tile_b/1e3:.1f} kB/tile  one-day place = 9 x {tile_b/1e3:.1f} kB x {blocks} blocks = {9*tile_b*blocks/1e6:.2f} MB")
    k = geo["keys"]
    lat, lon = geo["lat"], geo["lon"]
    nx, ny = int(k["Nx"]), int(k["Ny"])
    print("GRID KEYS", k)
    for (i, j) in ((0, 0), (900, 530), (nx - 1, ny - 1)):
        n = j * nx + i
        print(f"idx i={i} j={j} (flat j*Nx+i={n}): lat {lat[n]:.6f} lon {lon[n]:.6f}")


if __name__ == "__main__":
    main()
