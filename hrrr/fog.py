# hrrr/fog.py — read HRRR's fog-relevant pressure-level fields and sample them
# onto a lat/lon lattice.
#
# Fields come from the wrfprsf file by idx + byte range (as hrrr/grib.py does):
# T, RH and height at 1000..700 mb, plus 2 m T/RH and surface height. The grid is
# HRRR's Lambert conformal; lcc_ij inverts it so a lat/lon lattice can be
# bilinearly sampled from the native 3 km cells.
#
# read_fog_fields contract: every field, "lat" and "lon" is a FLAT 1-D array in the
# GRIB's order, n = j * Nx + i with i (along x) fastest and the first point at the
# south-west corner; reshape(Ny, Nx) gives values2d[j, i] for sample_bilinear.
# "Nx", "Ny" and "grid" (the grid_keys dict, ready for lcc_params) come from the
# first message.
import math

import numpy as np
import eccodes

_LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700)
FOG_FIELDS = tuple(
    f for p in _LEVELS for f in (("TMP", f"{p} mb"), ("RH", f"{p} mb"), ("HGT", f"{p} mb"))
) + (("TMP", "2 m above ground"), ("RH", "2 m above ground"), ("HGT", "surface"))

EARTH_R = 6371229.0  # HRRR's sphere
_PRS = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{d}/conus/hrrr.t{h:02d}z.wrfprsf{f:02d}.grib2"


def prs_url(run, fh):
    return _PRS.format(d=run.strftime("%Y%m%d"), h=run.hour, f=fh)


def fog_ranges(idx_text, fh):
    """{(name, level): (start, end)} for FOG_FIELDS at forecast hour fh (f00 reads
    "anl"); end inclusive, None for the last message. KeyError if one is missing."""
    want = "anl" if fh == 0 else f"{fh} hour fcst"
    rows = [ln.split(":") for ln in idx_text.strip().split("\n") if ln.strip()]
    out = {}
    for name, level in FOG_FIELDS:
        k = next((i for i, r in enumerate(rows) if len(r) > 5 and r[3] == name
                  and r[4] == level and r[5] == want), None)
        if k is None:
            raise KeyError(f"{name} ({level}, {want}) not in idx")
        out[(name, level)] = (int(rows[k][1]), int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None)
    return out


def read_fog_fields(run, fh, get):
    """{"lat", "lon", (name, level): ndarray} for one forecast hour. lon in -180..180,
    T in deg C, RH %, HGT m; NaN where missing. get(url, rng=None) -> bytes is injected."""
    url = prs_url(run, fh)
    ranges = fog_ranges(get(url + ".idx").decode(), fh)
    out = {}
    for key in FOG_FIELDS:
        g = eccodes.codes_new_from_message(get(url, ranges[key]))
        try:
            vals = eccodes.codes_get_values(g)
            miss = eccodes.codes_get(g, "missingValue")
            if "lat" not in out:
                out["grid"] = grid_keys(g)
                out["Nx"], out["Ny"] = int(out["grid"]["Nx"]), int(out["grid"]["Ny"])
                lat = eccodes.codes_get_array(g, "latitudes")
                lon = eccodes.codes_get_array(g, "longitudes")
                out["lat"] = lat
                out["lon"] = np.where(lon > 180, lon - 360, lon)
        finally:
            eccodes.codes_release(g)
        v = np.where(vals == miss, np.nan, vals.astype(float))
        out[key] = v - 273.15 if key[0] == "TMP" else v
    return out


def grid_keys(msg):
    """The GRIB keys lcc_params needs, read from a message handle."""
    names = ("Latin1InDegrees", "Latin2InDegrees", "LoVInDegrees",
             "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
             "DxInMetres", "DyInMetres", "Nx", "Ny")
    return {k: eccodes.codes_get(msg, k) for k in names}


def _t(phi):
    return math.tan(math.pi / 4 + phi / 2)


def lcc_params(k):
    """Spherical Lambert conformal constants plus the first point's projected x/y."""
    p1, p2 = math.radians(k["Latin1InDegrees"]), math.radians(k["Latin2InDegrees"])
    lov = k["LoVInDegrees"]
    if abs(p1 - p2) < 1e-12:
        n = math.sin(p1)  # tangent case
    else:
        n = math.log(math.cos(p1) / math.cos(p2)) / math.log(_t(p2) / _t(p1))
    f = math.cos(p1) * _t(p1) ** n / n
    p = {"n": n, "F": f, "lov": lov, "dx": k["DxInMetres"], "dy": k["DyInMetres"],
         "nx": int(k["Nx"]), "ny": int(k["Ny"])}
    x1, y1 = _xy(math.radians(k["latitudeOfFirstGridPointInDegrees"]),
                 k["longitudeOfFirstGridPointInDegrees"], p)
    p["x1"], p["y1"] = x1, y1
    return p


def _xy(phi, lon_deg, p):
    rho = EARTH_R * p["F"] / _t(phi) ** p["n"]
    d = (lon_deg - p["lov"] + 180.0) % 360.0 - 180.0  # wrap so -100 and 260 agree
    th = p["n"] * math.radians(d)
    return rho * math.sin(th), -rho * math.cos(th)


def lcc_ij(lat, lon, p):
    """Fractional grid indices (fi along x, fj along y, from the first point) of
    lat/lon in degrees; lon may be -180..180 or 0..360. Accepts scalars or arrays."""
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    rho = EARTH_R * p["F"] / np.tan(np.pi / 4 + np.radians(lat) / 2) ** p["n"]
    th = p["n"] * np.radians((lon - p["lov"] + 180.0) % 360.0 - 180.0)
    x, y = rho * np.sin(th), -rho * np.cos(th)
    return (x - p["x1"]) / p["dx"], (y - p["y1"]) / p["dy"]


def sample_bilinear(values2d, fi, fj):
    """values2d[j, i] sampled at fractional (fi, fj); NaN outside the grid or when
    any of the four cells is NaN."""
    v = np.asarray(values2d, dtype=float)
    fi, fj = np.asarray(fi, dtype=float), np.asarray(fj, dtype=float)
    ny, nx = v.shape
    inside = (fi >= 0) & (fi <= nx - 1) & (fj >= 0) & (fj <= ny - 1)
    i0 = np.clip(np.floor(np.where(inside, fi, 0)).astype(int), 0, nx - 2)
    j0 = np.clip(np.floor(np.where(inside, fj, 0)).astype(int), 0, ny - 2)
    a = np.where(inside, fi, 0) - i0
    b = np.where(inside, fj, 0) - j0
    r = ((1 - a) * (1 - b) * v[j0, i0] + a * (1 - b) * v[j0, i0 + 1]
         + (1 - a) * b * v[j0 + 1, i0] + a * b * v[j0 + 1, i0 + 1])
    return np.where(inside, r, np.nan)
