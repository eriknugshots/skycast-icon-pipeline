# tests/gribfake.py — a fake NOAA file server for the feed tests: real GRIB2
# messages laid end to end, an .idx whose offsets are true, and a get() that
# serves byte ranges exactly as S3/NOMADS do.
import numpy as np
import eccodes


def msg(values, ni=2, nj=2, lat_first=41.0, lat_last=40.0, lon_first=250.0, lon_last=251.0, step=1.0):
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
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

    def get(self, url, rng=None):
        self.calls.append((url, rng))
        if url in self.idx:
            return self.idx[url]
        blob = self.blobs[url]
        if rng is None:
            return blob
        s, e = rng
        return blob[s:] if e is None else blob[s:e + 1]
