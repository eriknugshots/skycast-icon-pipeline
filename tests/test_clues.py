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
    assert len(b) == 27 == clues.N_BYTES
    back = decode_clues(b)
    assert abs(back["decks"]["mid"]["depth"] - deck_clues(pr)["mid"]["depth"]) <= 5.0 + 1e-9


def test_node_convection_fields():
    pr = cp.clear_dry()
    pr.update(cape_con=400.0, hbas_con=1500.0, htop_con=9000.0)
    n = node_clues(pr)
    assert n["convCape"] == 400.0 and n["convBase"] == 1.5 and n["convTop"] == 9.0
    none = node_clues(cp.clear_dry())
    assert none["convBase"] == 0.0 and none["convTop"] == 0.0


def test_bytes_clamp_and_dry_layer():
    d = deck_clues(cp.clear_dry())
    assert d["mid"]["depth"] == 0.0 and math.isnan(d["mid"]["capTheta"])
    b = clues._byte("convCape", 99999.0)
    assert b == 254


def test_fixture_matches_code():
    with open(FIXTURE) as f:
        fx = json.load(f)
    assert len(fx["profiles"]) == 8
    for entry in fx["profiles"]:
        pr = {k: np.array([np.nan if x is None else x for x in v], dtype=float)
              if isinstance(v, list) else v for k, v in entry["profile"].items()}
        got = encode_clues(deck_clues(pr), node_clues(pr))
        assert list(got) == entry["bytes"], entry["name"]
        assert decode_clues(got) == entry["decoded"], entry["name"]
