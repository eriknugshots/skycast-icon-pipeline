# rrfs/grib.py — fetch NOAA RRFS's cloud-cover fields from NOMADS.
#
# File: the 13 km "2dfld" product on its North America grid (one rotated
# lat/lon grid, valid 3.8-90 N). The 3 km CONUS subset is HRRR's own grid and
# would not reach Seattle or Miami. Against the 3 km mean at the feed's nodes
# the 13 km file has correlation 0.998, mean difference 0.8 points (plan
# 2026-11-09). Cover costs 1.54 MB a forecast hour.
#
# Layer heights are NOAA UPP's, as HRRR's and GFS's: low = ground to 642 hPa,
# mid 642-350, high 350-150.
#
# Each field is one GRIB2 message; the .idx sidecar lists every message's byte
# offset, so we ask for just the four we want. The file also carries averaged
# rows ("5-6 hour ave fcst") for the same field names: only the instantaneous
# row for the hour is wanted.
import numpy as np
import eccodes

# (short name, level text in the idx) in the feed's low / mid / high / total order.
FIELDS = (
    ("LCDC", "low cloud layer"),
    ("MCDC", "middle cloud layer"),
    ("HCDC", "high cloud layer"),
    ("TCDC", "entire atmosphere (considered as a single layer)"),
)
_BASE = ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/prod/rrfs.{d}/{h:02d}/"
         "rrfs.t{h:02d}z.2dfld.13km.f{f:03d}.na.grib2")


def file_url(run, fh):
    return _BASE.format(d=run.strftime("%Y%m%d"), h=run.hour, f=fh)


def field_ranges(idx_text, fh):
    """Byte range of each wanted field for forecast hour fh: {short name: (start, end)}.

    The row must say "<fh> hour fcst" ("anl" at f000). end is inclusive (the
    next message's start - 1), or None for the last message in the file."""
    want = "anl" if fh == 0 else f"{fh} hour fcst"
    rows = [ln.split(":") for ln in idx_text.strip().split("\n") if ln.strip()]
    out = {}
    for name, level in FIELDS:
        k = next((i for i, r in enumerate(rows)
                  if len(r) > 5 and r[3] == name and r[4] == level and r[5] == want), None)
        if k is None:
            raise KeyError(f"{name} ({level}, {want}) not in idx")
        start = int(rows[k][1])
        end = int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None
        out[name] = (start, end)
    return out


def read_fields(run, fh, get):
    """Decode the four cloud fields for one forecast hour.

    get(url, rng=None) -> bytes is injected (rng = (start, end), end None for an
    open range). Returns {"lat", "lon", "LCDC", ...}: lon in -180..180, one float
    array per field, NaN where missing or negative."""
    url = file_url(run, fh)
    ranges = field_ranges(get(url + ".idx").decode(), fh)
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
