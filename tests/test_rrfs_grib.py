from datetime import datetime, timezone

import numpy as np
import pytest

from rrfs.grib import FIELDS, field_ranges, file_url, read_fields
from tests.gribfake import Files, msg

RUN = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
TCDC_LEVEL = "entire atmosphere (considered as a single layer)"

# The first row is copied from a live idx (NOMADS, 2026-11-09 12Z, f006); the rest
# follow its layout. Averaged rows (5-6 hour ave fcst) sit before the instantaneous
# ones on purpose: they must never be picked.
IDX = "\n".join([
    "10:900:d=2026110912:PRES:surface:6 hour fcst:",
    "11:1000:d=2026110912:LCDC:low cloud layer:5-6 hour ave fcst:",
    "154:54103011:d=2026110912:LCDC:low cloud layer:6 hour fcst:",
    "155:54200000:d=2026110912:MCDC:middle cloud layer:5-6 hour ave fcst:",
    "156:54300000:d=2026110912:MCDC:middle cloud layer:6 hour fcst:",
    "157:54400000:d=2026110912:HCDC:high cloud layer:6 hour fcst:",
    "158:54500000:d=2026110912:TCDC:entire atmosphere (considered as a single layer):5-6 hour ave fcst:",
    "159:54600000:d=2026110912:TCDC:entire atmosphere (considered as a single layer):6 hour fcst:",
    "160:54700000:d=2026110912:TMP:2 m above ground:6 hour fcst:",
]) + "\n"


def test_file_url():
    assert file_url(RUN, 6) == ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/prod/rrfs.20261109/12/"
                                "rrfs.t12z.2dfld.13km.f006.na.grib2")
    assert file_url(RUN, 84).endswith("rrfs.t12z.2dfld.13km.f084.na.grib2")


def test_instantaneous_rows_only():
    r = field_ranges(IDX, 6)
    assert r["LCDC"] == (54103011, 54199999)
    assert r["MCDC"] == (54300000, 54399999)
    assert r["HCDC"] == (54400000, 54499999)
    assert r["TCDC"] == (54600000, 54699999)


def test_hour_text_must_match():
    with pytest.raises(KeyError, match="LCDC"):
        field_ranges(IDX, 7)


def test_f000_is_anl():
    idx = IDX.replace("6 hour fcst", "anl")
    assert field_ranges(idx, 0)["LCDC"][0] == 54103011
    with pytest.raises(KeyError):
        field_ranges(idx, 6)


def test_last_row_is_open_range():
    idx = "\n".join(l for l in IDX.splitlines() if "TMP" not in l) + "\n"
    assert field_ranges(idx, 6)["TCDC"] == (54600000, None)


def test_missing_field_raises():
    idx = "\n".join(l for l in IDX.splitlines() if ":HCDC:" not in l)
    with pytest.raises(KeyError, match="HCDC"):
        field_ranges(idx, 6)


def test_read_fields_uses_ranges():
    f = Files()
    rows = [("PRES", "surface", "6 hour fcst", msg([1, 2, 3, 4]))]
    for k, (n, lv) in enumerate(FIELDS):
        rows.append((n, lv, "5-6 hour ave fcst", msg([99] * 4)))
        rows.append((n, lv, "6 hour fcst", msg([10 + k, 20 + k, 30 + k, 40 + k])))
    f.add(file_url(RUN, 6), rows)
    out = read_fields(RUN, 6, f.get)
    assert set(out) == {"lat", "lon", "LCDC", "MCDC", "HCDC", "TCDC"}
    assert out["LCDC"].tolist() == [10, 20, 30, 40] and out["TCDC"].tolist() == [13, 23, 33, 43]
    assert sorted(out["lon"].tolist()) == [-110.0, -110.0, -109.0, -109.0]
    assert len(f.calls) == 5 and f.calls[0] == (file_url(RUN, 6) + ".idx", None)
