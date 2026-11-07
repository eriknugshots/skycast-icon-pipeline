from datetime import datetime, timezone

import numpy as np
import pytest
import eccodes

from hrrr.grib import FIELDS, field_ranges, file_url, read_fields

IDX = "\n".join([
    "1:0:d=2026110612:PRES:surface:5 hour fcst:",
    "2:1000:d=2026110612:LCDC:low cloud layer:5 hour fcst:",
    "3:2500:d=2026110612:MCDC:middle cloud layer:5 hour fcst:",
    "4:4000:d=2026110612:HCDC:high cloud layer:5 hour fcst:",
    "5:5200:d=2026110612:TMP:2 m above ground:5 hour fcst:",
    "6:6000:d=2026110612:TCDC:entire atmosphere:5 hour fcst:",
]) + "\n"


def test_field_ranges_bounds():
    r = field_ranges(IDX)
    assert r["LCDC"] == (1000, 2499)
    assert r["MCDC"] == (2500, 3999)
    assert r["HCDC"] == (4000, 5199)
    assert r["TCDC"] == (6000, None)


def test_field_ranges_missing_raises():
    idx = "\n".join(l for l in IDX.splitlines() if "HCDC" not in l)
    with pytest.raises(KeyError, match="HCDC"):
        field_ranges(idx)


def test_file_url():
    run = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    assert file_url(run, 5) == "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261106/conus/hrrr.t12z.wrfsfcf05.grib2"


def _msg(values):
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(h, "Ni", 2)
    eccodes.codes_set(h, "Nj", 2)
    eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", 41.0)
    eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", 40.0)
    eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", 250.0)
    eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", 251.0)
    eccodes.codes_set(h, "iDirectionIncrementInDegrees", 1.0)
    eccodes.codes_set(h, "jDirectionIncrementInDegrees", 1.0)
    eccodes.codes_set(h, "bitsPerValue", 24)
    eccodes.codes_set_values(h, np.array(values, dtype=float))
    b = eccodes.codes_get_message(h)
    eccodes.codes_release(h)
    return b


def test_read_fields_uses_ranges():
    run = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    url = file_url(run, 5)
    msgs = {
        "LCDC": _msg([10, 20, 30, 40]),
        "MCDC": _msg([11, 21, 31, 41]),
        "HCDC": _msg([12, 22, 32, 42]),
        "TCDC": _msg([13, 23, 33, 43]),
    }
    n = {k: len(v) for k, v in msgs.items()}
    # idx whose offsets match the canned message sizes
    off = {}
    pos = 0
    lines = []
    for i, (name, level) in enumerate(FIELDS, 1):
        off[name] = pos
        lines.append(f"{i}:{pos}:d=2026110612:{name}:{level}:5 hour fcst:")
        pos += n[name]
    idx = "\n".join(lines) + "\n"
    ranges = {k: (off[k], off[k] + n[k] - 1) for k in msgs}
    ranges["TCDC"] = (off["TCDC"], None)
    calls = []

    def get(u, rng=None):
        calls.append((u, rng))
        if u == url + ".idx":
            return idx.encode()
        for k, r in ranges.items():
            if rng == r:
                return msgs[k]
        raise AssertionError(f"unexpected {u} {rng}")

    out = read_fields(run, 5, get)
    assert calls[0] == (url + ".idx", None)
    assert sorted(c[1] for c in calls[1:]) == sorted(ranges.values())
    assert len(calls) == 5
    assert set(out) == {"lat", "lon", "LCDC", "MCDC", "HCDC", "TCDC"}
    assert out["LCDC"].shape == (4,) and out["lat"].shape == (4,)
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.0, -109.0]
    assert out["TCDC"].tolist() == [13, 23, 33, 43]
