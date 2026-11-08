# globalfeed/grib.py — byte-range readers for GFS (NCEP .idx) and AIFS (ECMWF
# .index JSON) cloud-cover fields. Same approach as hrrr/grib.py: read the
# sidecar index, then ask the server for just the messages we want.
import json

import numpy as np
import eccodes


def _key(f):
    return f[0] if isinstance(f, tuple) else f


def _ncep_ranges(model, index_text, fh):
    want = "anl" if fh == 0 else f"{fh} hour fcst"
    rows = [ln.split(":") for ln in index_text.strip().split("\n") if ln.strip()]
    out = {}
    for name, level in model.fields:
        k = next((i for i, r in enumerate(rows)
                  if len(r) > 5 and r[3] == name and r[4] == level and r[5] == want), None)
        if k is None:
            raise KeyError(f"{name} ({level}, {want}) not in idx")
        start = int(rows[k][1])
        end = int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None
        out[name] = (start, end)
    return out


def _ecmwf_ranges(model, index_text):
    rows = [json.loads(ln) for ln in index_text.strip().split("\n") if ln.strip()]
    out = {}
    for key in model.fields:
        r = next((r for r in rows if r.get("param") == key and r.get("levtype") == "sfc"), None)
        if r is None:
            raise KeyError(f"{key} (sfc) not in index")
        out[key] = (r["_offset"], r["_offset"] + r["_length"] - 1)
    return out


def field_ranges(model, index_text, fh):
    """Byte range of each wanted field: {key: (start, end)}; end is inclusive,
    or None for an open range (last message of an NCEP file)."""
    if model.index_kind == "ncep":
        return _ncep_ranges(model, index_text, fh)
    return _ecmwf_ranges(model, index_text)


def read_step(model, run, fh, get):
    """Decode the four cloud fields for one forecast hour.

    get(url, rng=None) -> bytes is injected. Returns {"lat", "lon", <key>...}:
    lon in -180..180, float arrays, NaN where missing or negative."""
    ranges = field_ranges(model, get(model.index_url(run, fh)).decode(), fh)
    url = model.url(run, fh)
    out = {}
    for f in model.fields:
        key = _key(f)
        g = eccodes.codes_new_from_message(get(url, ranges[key]))
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
        out[key] = np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals)
    return out
