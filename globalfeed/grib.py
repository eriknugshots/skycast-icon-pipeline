# globalfeed/grib.py — byte-range readers for GFS (NCEP .idx) and AIFS / IFS
# (ECMWF .index JSON) cloud-cover fields. Same approach as hrrr/grib.py: read the
# sidecar index, then ask the server for just the messages we want.
import json

import numpy as np
import eccodes

G = 9.80665
_MASS = (("ICMR", "ice"), ("CLMR", "water"))


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


def _ecmwf_ranges(model, index_text, keys=None):
    rows = [json.loads(ln) for ln in index_text.strip().split("\n") if ln.strip()]
    out = {}
    for key in (model.fields if keys is None else keys):
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
            scale = _cover_scale(g, key) if model.check_cover_units else 1.0
        finally:
            eccodes.codes_release(g)
        out[key] = np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals * scale)
    return out


# GRIB units of a cover field -> factor to percent.
_PERCENT = {"%": 1.0, "(0 - 1)": 100.0}


def _cover_scale(g, key):
    units = eccodes.codes_get(g, "units")
    if units not in _PERCENT:
        raise ValueError(f"{key}: cover in unexpected units {units!r}")
    return _PERCENT[units]


# ---- Thickness (ICT1) ---------------------------------------------------------
# GFS's ice (ICMR) and cloud water (CLMR) mixing ratios, kg/kg, on the pressure
# levels in model.thick_levels, integrated to kg/m2 with dp / g, dp from
# numpy.gradient of the levels in Pa (centred inside, one-sided at the ends).

def thick_ranges(model, index_text, fh):
    """{"ICMR": [(level hPa, (start, end)), ...], "CLMR": [...]}, levels
    ascending, rows picked by exact name, level and forecast text ("anl" at
    f000). end is inclusive or None for the file's last message. KeyError if a
    row is missing."""
    want = "anl" if fh == 0 else f"{fh} hour fcst"
    rows = [ln.split(":") for ln in index_text.strip().split("\n") if ln.strip()]
    out = {}
    for name, _ in _MASS:
        picked = []
        for lev in sorted(model.thick_levels):
            k = next((i for i, r in enumerate(rows) if len(r) > 5 and r[3] == name
                      and r[4] == f"{lev} mb" and r[5] == want), None)
            if k is None:
                raise KeyError(f"{name} ({lev} mb, {want}) not in idx")
            start = int(rows[k][1])
            end = int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else None
            picked.append((lev, (start, end)))
        out[name] = picked
    return out


def split_messages(buf):
    """Whole GRIB2 messages laid end to end -> [message bytes]; each message
    states its own length in bytes 8..15."""
    out, pos = [], 0
    while pos < len(buf):
        if buf[pos:pos + 4] != b"GRIB":
            raise ValueError(f"not a GRIB message at byte {pos}")
        n = int.from_bytes(buf[pos + 8:pos + 16], "big")
        out.append(buf[pos:pos + n])
        pos += n
    return out


def _groups(ranges):
    """Merge ranges that touch into one request: [((start, end), [positions])],
    positions in file order (NCEP lists 1000 mb first, the ranges here ascend by level)."""
    groups = []
    for i, (_, (s, e)) in sorted(enumerate(ranges), key=lambda t: t[1][1][0]):
        if groups and groups[-1][0][1] is not None and groups[-1][0][1] + 1 == s:
            groups[-1] = ((groups[-1][0][0], e), groups[-1][1] + [i])
        else:
            groups.append(((s, e), [i]))
    return groups


def _decode(g):
    vals = eccodes.codes_get_values(g)
    miss = eccodes.codes_get(g, "missingValue")
    return np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals)


def read_thick_step(model, run, fh, get):
    """Column cloud ice and water for one forecast hour, kg/m2, on the source
    grid: {"lat", "lon", "ice", "water"} (lon in -180..180). get(url, rng=None)
    -> bytes is injected. A model with thick_fields reads its column totals
    (read_column_thick); one with thick_levels integrates them here: messages
    whose idx rows touch are fetched as one range, a level missing at a cell
    adds nothing there, and a cell missing at every level is NaN."""
    if model.thick_fields:
        return read_column_thick(model, run, fh, get)
    if not model.thick_levels:
        raise ValueError(f"{model.name} has no thickness levels")
    ranges = thick_ranges(model, get(model.index_url(run, fh)).decode(), fh)
    url = model.url(run, fh)
    weights = np.gradient(np.asarray(sorted(model.thick_levels), dtype=float) * 100.0) / G
    out = {}
    for name, key in _MASS:
        acc, seen = None, None
        for rng, positions in _groups(ranges[name]):
            msgs = split_messages(get(url, rng))
            if len(msgs) != len(positions):
                raise ValueError(f"{name}: asked for {len(positions)} messages, got {len(msgs)}")
            for pos, raw in zip(positions, msgs):
                g = eccodes.codes_new_from_message(raw)
                try:
                    if "lat" not in out:
                        lat = eccodes.codes_get_array(g, "latitudes")
                        lon = eccodes.codes_get_array(g, "longitudes")
                        out["lat"] = lat
                        out["lon"] = np.where(lon > 180, lon - 360, lon)
                    v = _decode(g)
                finally:
                    eccodes.codes_release(g)
                good = ~np.isnan(v)
                if acc is None:
                    acc, seen = np.zeros_like(v), np.zeros(v.shape, dtype=bool)
                acc += np.where(good, v, 0.0) * weights[pos]
                seen |= good
        out[key] = np.where(seen, acc, np.nan)
    return out


# ---- Thickness from column fields (IFS) ---------------------------------------
# ECMWF's tciw / tclw are already the column's cloud ice and liquid water in
# kg/m2: one sfc message each, read from the same .index as the cover.
_KG_M2 = "kg m**-2"


def read_column_thick(model, run, fh, get):
    """{"lat", "lon", "ice", "water"} from model.thick_fields, kg/m2 as
    published (negative or missing values NaN). One range request per field.
    ValueError if a message is not in kg m**-2; KeyError if the index lacks a field."""
    keys = [k for k, _ in model.thick_fields]
    ranges = _ecmwf_ranges(model, get(model.index_url(run, fh)).decode(), keys)
    url = model.url(run, fh)
    out = {}
    for key, name in model.thick_fields:
        g = eccodes.codes_new_from_message(get(url, ranges[key]))
        try:
            units = eccodes.codes_get(g, "units")
            if units != _KG_M2:
                raise ValueError(f"{key}: expected {_KG_M2}, got {units!r}")
            if "lat" not in out:
                lat = eccodes.codes_get_array(g, "latitudes")
                lon = eccodes.codes_get_array(g, "longitudes")
                out["lat"] = lat
                out["lon"] = np.where(lon > 180, lon - 360, lon)
            out[name] = _decode(g)
        finally:
            eccodes.codes_release(g)
    return out
