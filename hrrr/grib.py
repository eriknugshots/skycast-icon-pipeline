# hrrr/grib.py — fetch HRRR's cloud-cover fields from NOAA's open S3 bucket.
#
# Layer heights, which differ from ICON's and must be remembered wherever the
# two feeds are compared: HRRR low = ground to 642 hPa, mid 642-350, high
# 350-150 (NOAA UPP INITPOST.F); ICON low = ground to 800 hPa, mid 800-400,
# high above 400.
#
# Each field is one GRIB2 message; the .idx sidecar lists every message's byte
# offset, so we ask S3 for just the four we want instead of the whole file.
import numpy as np
import eccodes

# (short name, level text in the idx) in the feed's low / mid / high / total order.
FIELDS = (
    ("LCDC", "low cloud layer"),
    ("MCDC", "middle cloud layer"),
    ("HCDC", "high cloud layer"),
    ("TCDC", "entire atmosphere"),
)
_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{d}/conus/hrrr.t{h:02d}z.wrfsfcf{f:02d}.grib2"


def file_url(run, fh):
    return _BASE.format(d=run.strftime("%Y%m%d"), h=run.hour, f=fh)


def field_ranges(idx_text):
    """Byte range of each wanted field: {short name: (start, end)}.

    end is inclusive (the next message's start - 1), or None for the last
    message in the file, which means an open range."""
    rows = [ln.split(":") for ln in idx_text.strip().split("\n") if ln.strip()]
    out = {}
    for name, level in FIELDS:
        k = next((i for i, r in enumerate(rows) if len(r) > 4 and r[3] == name and r[4] == level), None)
        if k is None:
            raise KeyError(f"{name} ({level}) not in idx")
        start = int(rows[k][1])
        end = int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None
        out[name] = (start, end)
    return out


# ---- Thickness (ICT1) ---------------------------------------------------------
# Ice is TCOLI plus the snow column: HRRR's microphysics keeps most of its ice
# mass in the snow category, and TCOLI alone reads about a third of ICON's on
# the same decks (plan 2026-11-09, measured against GOES CODC). Snow is SNMR
# (kg/kg) from the pressure-level file at every other level from 100 mb,
# integrated with dp from numpy.gradient of those levels; every other level
# matches the full integral for RRFS's 25 mb spacing (ice 1.000, water 1.005).
G = 9.80665
SNOW_LEVELS = tuple(range(100, 1001, 50))
_PRS_BASE = _BASE.replace("wrfsfc", "wrfprs")
_SFC_THICK = ("TCOLI", "TCOLW")


def prs_url(run, fh):
    return _PRS_BASE.format(d=run.strftime("%Y%m%d"), h=run.hour, f=fh)


def layer_weights(levels_hpa):
    """kg/m2 of air per unit mixing ratio, one per level: dp / g with dp from
    numpy.gradient of the levels in Pa (centred inside, one-sided at the ends)."""
    return np.gradient(np.asarray(levels_hpa, dtype=float) * 100.0) / G


def _rows(idx_text):
    return [ln.split(":") for ln in idx_text.strip().split("\n") if ln.strip()]


def _range_of(rows, k):
    start = int(rows[k][1])
    return start, (int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None)


def thick_ranges(sfc_idx, prs_idx, fh):
    """{"TCOLI": (s, e), "TCOLW": (s, e), "SNMR": [(level hPa, (s, e)), ...]}
    for forecast hour fh (f00 reads "anl"). KeyError if anything is missing."""
    want = "anl" if fh == 0 else f"{fh} hour fcst"
    out = {}
    rows = _rows(sfc_idx)
    for name in _SFC_THICK:
        k = next((i for i, r in enumerate(rows) if len(r) > 5 and r[3] == name
                  and r[4].startswith("entire atmosphere") and r[5] == want), None)
        if k is None:
            raise KeyError(f"{name} (entire atmosphere, {want}) not in idx")
        out[name] = _range_of(rows, k)
    rows = _rows(prs_idx)
    snow = []
    for lev in SNOW_LEVELS:
        k = next((i for i, r in enumerate(rows) if len(r) > 5 and r[3] == "SNMR"
                  and r[4] == f"{lev} mb" and r[5] == want), None)
        if k is None:
            raise KeyError(f"SNMR ({lev} mb, {want}) not in idx")
        snow.append((lev, _range_of(rows, k)))
    out["SNMR"] = snow
    return out


def _decode(raw):
    g = eccodes.codes_new_from_message(raw)
    try:
        vals = eccodes.codes_get_values(g)
        miss = eccodes.codes_get(g, "missingValue")
    finally:
        eccodes.codes_release(g)
    return np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals)


def read_thick(run, fh, get):
    """Column cloud ice and water for one forecast hour, in kg/m2, on the same
    grid and cell order as read_fields: {"ice": TCOLI + snow, "water": TCOLW}.

    get(url, rng=None) -> bytes is injected. NaN where TCOLI or TCOLW is missing;
    a snow level that is missing at a cell adds nothing there. The snow column
    is accumulated one level at a time, so a worker holds a few arrays, not 19."""
    sfc, prs = file_url(run, fh), prs_url(run, fh)
    r = thick_ranges(get(sfc + ".idx").decode(), get(prs + ".idx").decode(), fh)
    ice = _decode(get(sfc, r["TCOLI"]))
    water = _decode(get(sfc, r["TCOLW"]))
    levels = [lev for lev, _ in r["SNMR"]]
    w = layer_weights(levels)
    snow = np.zeros_like(ice)
    for wt, (_, rng) in zip(w, r["SNMR"]):
        v = _decode(get(prs, rng))
        snow += np.where(np.isnan(v), 0.0, v) * wt
    return {"ice": ice + snow, "water": water}


def read_fields(run, fh, get):
    """Decode the four cloud fields for one forecast hour.

    get(url, rng=None) -> bytes is injected (rng = (start, end), end None for
    an open range). Returns {"lat", "lon", "LCDC", ...}: lon in -180..180, one
    float array per field, NaN where missing or negative."""
    url = file_url(run, fh)
    ranges = field_ranges(get(url + ".idx").decode())
    out = {}
    for name, _ in FIELDS:
        g = eccodes.codes_new_from_message(get(url, ranges[name]))
        try:
            vals = eccodes.codes_get_values(g)
            if "lat" not in out:
                lat = eccodes.codes_get_array(g, "latitudes")
                lon = eccodes.codes_get_array(g, "longitudes")
                out["lat"] = lat
                out["lon"] = np.where(lon > 180, lon - 360, lon)
            miss = eccodes.codes_get(g, "missingValue")
        finally:
            eccodes.codes_release(g)
        out[name] = np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals)
    return out
