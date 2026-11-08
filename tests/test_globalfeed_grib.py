import json
from datetime import datetime, timezone

import numpy as np
import pytest
import eccodes

from globalfeed.models import GFS, AIFS
from globalfeed.grib import field_ranges, read_step

RUN = datetime(2026, 11, 7, 0, tzinfo=timezone.utc)
G = GFS.fields


def _ncep(fh_text, order, tail_rows=True):
    """idx where every field has an instantaneous row and an 'ave' row."""
    rows, pos = [], 100
    for key, level in G:
        pair = [f"{key}:{level}:{fh_text}:", f"{key}:{level}:0-6 hour ave fcst:"]
        if order == "ave_first":
            pair.reverse()
        for p in pair:
            rows.append((pos, p))
            pos += 1000
    return "\n".join(f"{i}:{o}:d=2026110700:{p}" for i, (o, p) in enumerate(rows, 1)) + "\n", rows


def _inst_offsets(rows, fh_text):
    return {key: next(o for o, p in rows if p.startswith(f"{key}:{lvl}:{fh_text}:"))
            for key, lvl in G}


@pytest.mark.parametrize("order", ["inst_first", "ave_first"])
def test_ncep_picks_instantaneous(order):
    idx, rows = _ncep("6 hour fcst", order)
    r = field_ranges(GFS, idx, 6)
    want = _inst_offsets(rows, "6 hour fcst")
    for key, _ in G:
        assert r[key][0] == want[key]


def test_ncep_f000_anl():
    idx, rows = _ncep("anl", "inst_first")
    r = field_ranges(GFS, idx, 0)
    want = _inst_offsets(rows, "anl")
    assert {k: r[k][0] for k, _ in G} == want


def test_ncep_missing_instantaneous_raises():
    idx = "\n".join([
        "1:0:d=2026110700:LCDC:low cloud layer:6 hour fcst:",
        "2:10:d=2026110700:MCDC:middle cloud layer:6 hour fcst:",
        "3:20:d=2026110700:HCDC:high cloud layer:6 hour fcst:",
        "4:30:d=2026110700:TCDC:entire atmosphere:0-6 hour ave fcst:",
    ]) + "\n"
    with pytest.raises(KeyError, match="TCDC"):
        field_ranges(GFS, idx, 6)


def test_ncep_end_is_next_start_minus_one_or_none():
    idx, rows = _ncep("6 hour fcst", "inst_first")
    r = field_ranges(GFS, idx, 6)
    nxt = {o: rows[i + 1][0] for i, (o, _) in enumerate(rows[:-1])}
    for key, _ in G:
        assert r[key][1] == nxt[r[key][0]] - 1 or r[key][1] is None


def _ecmwf(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _row(param, off, ln, levtype="sfc", **kw):
    d = {"domain": "g", "date": "20261107", "time": "0000", "step": "6",
         "levtype": levtype, "param": param, "_offset": off, "_length": ln}
    d.update(kw)
    return d


def test_ecmwf_index_ranges():
    rows = [_row(p, 1000 * (i + 1), 77 + i) for i, p in enumerate(AIFS.fields)]
    rows.insert(0, _row("lcc", 5, 9, levtype="pl", levelist="500"))
    r = field_ranges(AIFS, _ecmwf(rows), 6)
    for i, p in enumerate(AIFS.fields):
        assert r[p] == (1000 * (i + 1), 1000 * (i + 1) + 77 + i - 1)
    miss = [x for x in rows if x["param"] != "mcc"]
    with pytest.raises(KeyError, match="mcc"):
        field_ranges(AIFS, _ecmwf(miss), 6)


def test_steps():
    assert GFS.main_steps == tuple(range(0, 121))
    assert GFS.tail_steps == tuple(range(123, 145, 3))
    assert AIFS.main_steps == tuple(range(0, 121, 6))
    assert AIFS.tail_steps == tuple(range(126, 145, 6))


def test_frozen_and_workers_and_urls():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        GFS.name = "x"
    assert (GFS.workers, AIFS.workers) == (8, 4)
    assert GFS.url(RUN, 6) == "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261107/00/atmos/gfs.t00z.pgrb2.0p25.f006"
    assert GFS.index_url(RUN, 6) == GFS.url(RUN, 6) + ".idx"
    assert AIFS.url(RUN, 6) == "https://data.ecmwf.int/forecasts/20261107/00z/aifs-single/0p25/oper/20261107000000-6h-oper-fc.grib2"
    assert AIFS.index_url(RUN, 6) == AIFS.url(RUN, 6)[:-len(".grib2")] + ".index"


def _msg(values):
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in (("Ni", 2), ("Nj", 2), ("latitudeOfFirstGridPointInDegrees", 41.0),
                 ("latitudeOfLastGridPointInDegrees", 40.0),
                 ("longitudeOfFirstGridPointInDegrees", 250.0),
                 ("longitudeOfLastGridPointInDegrees", 251.0),
                 ("iDirectionIncrementInDegrees", 1.0), ("jDirectionIncrementInDegrees", 1.0),
                 ("bitsPerValue", 24)):
        eccodes.codes_set(h, k, v)
    eccodes.codes_set_values(h, np.array(values, dtype=float))
    b = eccodes.codes_get_message(h)
    eccodes.codes_release(h)
    return b


@pytest.mark.parametrize("model", [GFS, AIFS])
def test_read_step_requests_one_range_per_field(model):
    keys = [f[0] if isinstance(f, tuple) else f for f in model.fields]
    msgs = {k: _msg([10 * (i + 1) + j for j in range(4)]) for i, k in enumerate(keys)}
    pos, off = 0, {}
    for k in keys:
        off[k] = pos
        pos += len(msgs[k])
    ranges = {k: (off[k], off[k] + len(msgs[k]) - 1) for k in keys}
    if model.index_kind == "ncep":
        idx = "".join(f"{i}:{off[k]}:d=2026110700:{k}:{lvl}:6 hour fcst:\n"
                      for i, (k, lvl) in enumerate(model.fields, 1))
        ranges[keys[-1]] = (off[keys[-1]], None)
    else:
        idx = _ecmwf([_row(k, off[k], len(msgs[k])) for k in keys])
    calls = []

    def get(u, rng=None):
        calls.append((u, rng))
        if u == model.index_url(RUN, 6):
            return idx.encode()
        for k, r in ranges.items():
            if rng == r:
                return msgs[k]
        raise AssertionError(f"unexpected {u} {rng}")

    out = read_step(model, RUN, 6, get)
    assert calls[0] == (model.index_url(RUN, 6), None)
    assert all(c[0] == model.url(RUN, 6) for c in calls[1:])
    assert sorted(c[1] for c in calls[1:]) == sorted(ranges.values())
    assert len(calls) == 1 + len(keys)
    assert set(out) == {"lat", "lon", *keys}
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.0, -109.0]
    assert out[keys[0]].shape == (4,)
