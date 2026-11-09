# tests/gribfake.py — a fake NOAA / ECMWF file server for the feed tests: real
# GRIB2 messages laid end to end, an .idx (NCEP text) or .index (ECMWF JSON
# lines) whose offsets are true, and a get() that serves byte ranges exactly as
# S3/NOMADS/data.ecmwf.int do.
import json

import numpy as np
import eccodes


def msg(values, ni=2, nj=2, lat_first=41.0, lat_last=40.0, lon_first=250.0, lon_last=251.0, step=1.0,
        param=None):
    """param: an ecCodes paramId (int) or shortName (str) for the message;
    None leaves the sample's own (temperature, K)."""
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    if param is not None:
        eccodes.codes_set(h, "paramId" if isinstance(param, int) else "shortName", param)
    eccodes.codes_set(h, "Ni", ni)
    eccodes.codes_set(h, "Nj", nj)
    eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", lat_first)
    eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", lat_last)
    eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon_first)
    eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", lon_last)
    eccodes.codes_set(h, "iDirectionIncrementInDegrees", step)
    eccodes.codes_set(h, "jDirectionIncrementInDegrees", step)
    eccodes.codes_set(h, "bitsPerValue", 24)
    eccodes.codes_set_values(h, np.array(values, dtype=float))
    b = eccodes.codes_get_message(h)
    eccodes.codes_release(h)
    return b


class Files:
    """add(url, rows): rows = [(name, level, fcst, message bytes)], written in
    order; an .idx in NCEP's layout is served beside the file."""

    def __init__(self):
        self.blobs, self.idx, self.calls = {}, {}, []

    def add(self, url, rows, idx_suffix=".idx"):
        pos, lines, blob = 0, [], b""
        for i, (name, level, fcst, data) in enumerate(rows, 1):
            lines.append(f"{i}:{pos}:d=2026110912:{name}:{level}:{fcst}:")
            blob += data
            pos += len(data)
        self.blobs[url] = blob
        self.idx[url + idx_suffix] = ("\n".join(lines) + "\n").encode()

    def add_ecmwf(self, url, index_url, rows, date="20261108", time="0000", step="6"):
        """rows = [(param, levtype, message bytes)] or (param, levtype, levelist,
        message bytes), written in order; the .index beside the file has one
        JSON object per message in the layout of ECMWF's open-data .index files
        (written from ECMWF's documentation, not copied from a live file)."""
        pos, lines, blob = 0, [], b""
        for r in rows:
            param, levtype, data = r[0], r[1], r[-1]
            d = {"domain": "g", "date": date, "time": time, "expver": "0001", "class": "od",
                 "type": "fc", "stream": "oper", "step": step, "levtype": levtype, "param": param}
            if len(r) == 4:
                d["levelist"] = str(r[2])
            d["_offset"], d["_length"] = pos, len(data)
            lines.append(json.dumps(d))
            blob += data
            pos += len(data)
        self.blobs[url] = blob
        self.idx[index_url] = ("\n".join(lines) + "\n").encode()

    def get(self, url, rng=None):
        self.calls.append((url, rng))
        if url in self.idx:
            return self.idx[url]
        blob = self.blobs[url]
        if rng is None:
            return blob
        s, e = rng
        return blob[s:] if e is None else blob[s:e + 1]
