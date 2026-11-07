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
