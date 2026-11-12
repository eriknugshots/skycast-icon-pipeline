"""Write tests/fixtures/clues_fixture.json: 6 synthetic profiles + 2 real ICON nodes.

usage: python tools/make_clues_fixture.py <icon_or.npz> [out.json]
The npz is an Oregon cut of ICON (T/RELHUM/U/V/FI at 600-200 hPa; FI is already
geopotential height in m despite its name)."""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.clues import deck_clues, node_clues, encode_clues, decode_clues  # noqa: E402
from tests import clues_profiles as cp  # noqa: E402


def _list(a):
    return [None if np.isnan(x) else round(float(x), 6) for x in np.asarray(a, dtype=float)]


def real_nodes(npz):
    d = np.load(npz)
    plev = d["plev"].astype(float)          # 600..200
    step = 0
    T, RH, U, V, FI = (d[k][step] for k in ("T", "RELHUM", "U", "V", "FI"))
    mid_rh = RH[1:4].mean(axis=0)           # 500..300 hPa
    flat = np.where(np.isnan(mid_rh), np.nan, mid_rh)
    moist = np.unravel_index(np.nanargmax(flat), flat.shape)
    dry = np.unravel_index(np.nanargmin(flat), flat.shape)
    out = {}
    for name, (i, j) in (("icon_moist_aloft", moist), ("icon_dry_aloft", dry)):
        nan = lambda n: np.full(n, np.nan)
        p = np.concatenate([[1000, 925, 850, 700], plev])
        col = lambda f: np.concatenate([nan(4), f[:, i, j]])
        out[name] = {
            "p": p, "t": col(T), "rh": col(RH), "gh": col(FI), "u": col(U), "v": col(V),
            "cape_con": 0.0, "cape_ml": 0.0, "hbas_con": -500.0, "htop_con": -500.0,
            "_node": [float(d["lat"][i]), float(d["lon"][j])],
        }
    return out


def main():
    npz = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(__file__), "..", "tests", "fixtures", "clues_fixture.json")
    profiles = dict(cp.all_synthetic())
    profiles.update(real_nodes(npz))
    entries = []
    for name, pr in profiles.items():
        node = pr.pop("_node", None)
        # round-trip through the JSON precision so tests and fixture agree exactly
        pr = {k: (np.array(_list(v), dtype=float) if isinstance(v, np.ndarray) else v)
              for k, v in pr.items()}
        pr = {k: (np.array([np.nan if x is None else x for x in _list(v)]) if isinstance(v, np.ndarray) else v)
              for k, v in pr.items()}
        b = encode_clues(deck_clues(pr), node_clues(pr))
        e = {"name": name,
             "profile": {k: (_list(v) if isinstance(v, np.ndarray) else v) for k, v in pr.items()},
             "decoded": decode_clues(b), "bytes": list(b)}
        if node:
            e["icon_lat_lon"] = node
        entries.append(e)
    with open(out_path, "w") as f:
        json.dump({"note": "profile -> 27 clue bytes; decoded uses physical units, null = missing (255)",
                   "profiles": entries}, f, indent=1)
    print(len(entries), "profiles ->", out_path)


if __name__ == "__main__":
    main()
