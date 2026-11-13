import json
import math
import os

import numpy as np

from pipeline import clues
from pipeline.clues import deck_clues, node_clues, encode_clues, decode_clues
from tests import clues_profiles as cp

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "clues_fixture.json")


def test_thin_lid_mid_deck():
    mid = deck_clues(cp.thin_lid_mid())["mid"]
    assert 200 <= mid["depth"] <= 1200
    assert mid["capTheta"] >= 2.0
    assert mid["capRh"] >= 40


def test_deep_moist_mid():
    mid = deck_clues(cp.deep_moist_mid())["mid"]
    assert mid["depth"] >= 2000
    assert mid["capTheta"] < 1.0


def test_shear_direction():
    high = deck_clues(cp.shear_direction())["high"]
    assert abs(high["shearDir"] - 90.0) <= 2.0
    assert high["shear"] > 0


def test_rh_water_to_ice():
    # T -50 C, RHw 62 %: esw 6.382 Pa, esi 3.939 Pa -> 0.62 * 6.382 / 3.939 = 1.0046
    node = node_clues(cp.rh_water_to_ice_profile())
    assert abs(node["cruiseRhi"] - 1.0046) <= 0.01


def test_missing_level_gives_255():
    pr = cp.missing_upper()
    b = encode_clues(deck_clues(pr), node_clues(pr))
    high = b[14:21]
    assert list(high) == [255] * 7
    assert b[21 + 3] == 255  # cruiseRhi: no 300-200 hPa data


def test_encode_roundtrip():
    pr = cp.thin_lid_mid()
    b = encode_clues(deck_clues(pr), node_clues(pr))
    assert len(b) == 30 == clues.N_BYTES
    assert clues.N_BYTES_V1 == 27
    back = decode_clues(b)
    assert abs(back["decks"]["mid"]["depth"] - deck_clues(pr)["mid"]["depth"]) <= 5.0 + 1e-9


def test_node_convection_fields():
    pr = cp.clear_dry()
    pr.update(cape_con=400.0, hbas_con=1500.0, htop_con=9000.0)
    n = node_clues(pr)
    assert n["convCape"] == 400.0 and n["convBase"] == 1.5 and n["convTop"] == 9.0
    none = node_clues(cp.clear_dry())  # -500 = no convection: missing, not 0 km
    assert math.isnan(none["convBase"]) and math.isnan(none["convTop"])
    b = encode_clues(deck_clues(cp.clear_dry()), none)
    assert b[22] == 255 and b[23] == 255
    d = decode_clues(b)["node"]
    assert d["convBase"] is None and d["convTop"] is None


def test_bytes_clamp_and_dry_layer():
    d = deck_clues(cp.clear_dry())
    assert d["mid"]["depth"] == 0.0 and math.isnan(d["mid"]["capTheta"])
    b = clues._byte("convCape", 99999.0)
    assert b == 254


def test_fixture_matches_code():
    with open(FIXTURE) as f:
        fx = json.load(f)
    assert len(fx["profiles"]) == 9
    for entry in fx["profiles"]:
        pr = {k: np.array([np.nan if x is None else x for x in v], dtype=float)
              if isinstance(v, list) else v for k, v in entry["profile"].items()}
        got = encode_clues(deck_clues(pr), node_clues(pr))
        assert list(got) == entry["bytes"], entry["name"]
        assert decode_clues(got) == entry["decoded"], entry["name"]


def test_rounding_half_up_ties():
    assert clues._byte("wind", 1.25) == 3        # 2.5 -> 3 (Python round would give 2)
    assert clues._byte("wind", 0.25) == 1        # 0.5 -> 1
    assert clues._byte("cruiseU", -0.5) == 128   # 127.5 -> 128
    assert clues._byte("cruiseU", 0.5) == 129    # 128.5 -> 129
    assert clues._byte("capRh", 12.5) == 13
    assert clues._byte("wind", 1.2499) == 2


def test_byte_nonfinite_is_missing():
    for x in (np.float32("nan"), np.float32("inf"), float("-inf"), None):
        assert clues._byte("wind", x) == 255


def test_unrelated_nan_wind_does_not_change_moisture():
    ref = deck_clues(cp.thin_lid_mid())["mid"]
    pr = cp.thin_lid_mid()
    cp.at(pr, 500.0, u=np.nan)
    got = deck_clues(pr)["mid"]
    for k in ("depth", "capTheta", "capRh"):
        assert got[k] == ref[k]


def test_interior_nan_level_is_spanned():
    pr = cp.deep_moist_mid()
    cp.at(pr, 500.0, t=np.nan, rh=np.nan, gh=np.nan, u=np.nan, v=np.nan)
    assert deck_clues(pr)["mid"]["depth"] >= 2000


def test_run_at_edge_of_valid_data_is_missing():
    pr = cp.deep_moist_mid()
    for p in (1000.0, 925.0, 850.0, 700.0):   # nothing valid below 600 hPa
        cp.at(pr, p, t=np.nan, rh=np.nan, gh=np.nan, u=np.nan, v=np.nan)
    mid = deck_clues(pr)["mid"]
    assert math.isnan(mid["depth"]) and math.isnan(mid["capTheta"]) and math.isnan(mid["capRh"])
    assert not math.isnan(mid["lapse"])        # wind/shear/lapse may still be given
    b = encode_clues(deck_clues(pr), node_clues(pr))
    assert list(b[7:10]) == [255, 255, 255]
    # a single NaN at 700 with valid data below it is NOT an edge: depth survives
    pr2 = cp.deep_moist_mid()
    cp.at(pr2, 700.0, t=np.nan, rh=np.nan, gh=np.nan)
    assert deck_clues(pr2)["mid"]["depth"] >= 2000


def _uniform(u, v):
    pr = cp.deep_moist_mid()
    pr["u"] = np.full_like(np.asarray(pr["u"], dtype=float), u)
    pr["v"] = np.full_like(np.asarray(pr["v"], dtype=float), v)
    return pr


def test_wind_dir_is_where_the_mean_wind_blows_toward():
    # shearDir's convention: degrees clockwise from north, the direction TOWARD.
    for (u, v), want in (((12.0, 0.0), 90.0), ((0.0, -7.0), 180.0), ((-5.0, 0.0), 270.0), ((0.0, 3.0), 0.0),
                         ((10.0, 10.0 * math.sqrt(3)), 30.0)):
        d = deck_clues(_uniform(u, v))
        for name in clues.DECK_NAMES:
            assert abs(d[name]["windDir"] - want) <= 1e-9, (u, v, name)
            assert abs(d[name]["wind"] - math.hypot(u, v)) <= 1e-9


def test_wind_dir_missing_when_calm_or_no_wind():
    calm = deck_clues(_uniform(0.0, 0.0))
    none = deck_clues(_uniform(np.nan, np.nan))
    for name in clues.DECK_NAMES:
        assert calm[name]["wind"] == 0.0 and math.isnan(calm[name]["windDir"])
        assert math.isnan(none[name]["wind"]) and math.isnan(none[name]["windDir"])
    b = encode_clues(calm, node_clues(_uniform(0.0, 0.0)))
    assert list(b[27:30]) == [255, 255, 255]


def test_wind_dir_bytes_are_appended_after_the_27():
    pr = cp.shear_direction()
    deck, node = deck_clues(pr), node_clues(pr)
    b = encode_clues(deck, node)
    assert [b[27], b[28], b[29]] == [clues._byte("windDir", deck[n]["windDir"]) for n in clues.DECK_NAMES]
    other = {n: dict(deck[n], windDir=x) for n, x in zip(clues.DECK_NAMES, (10.0, 100.0, 200.0))}
    ob = encode_clues(other, node)
    assert ob[:27] == b[:27]                      # every ICX1 offset stays where it was
    assert list(ob[27:]) == [7, 71, 142]          # floor(deg * 255 / 360 + 0.5)
    assert clues._byte("windDir", 359.9) == 254   # clamped: 255 stays "missing"
    assert clues.CLUE_LAYOUT["windDir"] == clues.CLUE_LAYOUT["shearDir"]
    back = decode_clues(ob)["decks"]
    assert abs(back["mid"]["windDir"] - 71 * 360 / 255) <= 1e-9
    # a 27-byte record (ICX1) decodes with no wind direction, the rest unchanged
    v1 = decode_clues(ob[:27])
    assert all(v1["decks"][n]["windDir"] is None for n in clues.DECK_NAMES)
    assert {k: v for k, v in v1["decks"]["high"].items() if k != "windDir"} == \
           {k: v for k, v in back["high"].items() if k != "windDir"}
    assert v1["node"] == decode_clues(ob)["node"]


def test_fixture_has_wind_directions():
    with open(FIXTURE) as f:
        fx = json.load(f)
    tails = [e["bytes"][27:] for e in fx["profiles"]]
    assert all(len(t) == 3 for t in tails)
    assert any(all(x != 255 for x in t) for t in tails) and any(255 in t for t in tails)
