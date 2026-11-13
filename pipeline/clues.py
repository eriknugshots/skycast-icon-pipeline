# pipeline/clues.py — texture clues: 30 bytes per forecast column that tell the
# phone which cloud SHAPES the air can make, read from one profile (pressure
# levels of T, RH, height, wind) plus the model's own convection fields.
#
# Pure numpy, no I/O. Layout (255 = missing, bytes clamped to 0..254):
#   3 decks (low, mid, high) x 7 bytes, then 6 node bytes, then 3 tail bytes
#   (one per deck) = 30.
#   deck : depth, capTheta, capRh, shearDir, shear, wind, lapse
#   node : convCape, convBase, convTop, cruiseRhi, cruiseU, cruiseV
#   tail : windDir of low, mid, high (bytes 27, 28, 29)
# The tail was APPENDED (ICX2; ICX1 stopped at byte 27), so the first 27
# offsets are the same in both.
#
# Profile dict: p hPa (descending), t degC, rh % over WATER (ICON RELHUM),
# gh m (geopotential height, MSL), u, v m/s; singles cape_con, cape_ml,
# hbas_con, htop_con (m; -500 = none). A missing level is NaN.
import math
import numpy as np

DECK_NAMES = ("low", "mid", "high")
# Deck bands in km MSL. GUESS: tuned by eye, to be revisited against photos.
DECK_BANDS_KM = {"low": (0.3, 2.5), "mid": (2.5, 6.5), "high": (6.5, 13.0)}
DECK_CLUES = ("depth", "capTheta", "capRh", "shearDir", "shear", "wind", "lapse")
NODE_CLUES = ("convCape", "convBase", "convTop", "cruiseRhi", "cruiseU", "cruiseV")
# Per-deck clues carried AFTER the node bytes, deck by deck (low, mid, high).
DECK_TAIL_CLUES = ("windDir",)

# name -> (scale, offset). ROUNDING RULE (the JS twin must match exactly):
#   byte = clamp(floor(value * scale + offset + 0.5), 0, 254)   [half UP, not
#   Python's half-to-even]; byte = 255 only when the value is missing (None,
#   NaN or inf). cruiseU/V add their +128 offset BEFORE rounding. Example:
#   wind 1.25 m/s -> 1.25 * 2 = 2.5 -> byte 3. Decode is (byte - offset) / scale.
# Units in the comments.
CLUE_LAYOUT = {
    "depth": (0.1, 0.0),          # moist-layer depth, m
    "capTheta": (10.0, 0.0),      # theta jump above the layer top, K
    "capRh": (1.0, 0.0),          # RH(ice) drop just above, % (0..100)
    "shearDir": (255.0 / 360.0, 0.0),  # deg, direction the shear vector points
    "shear": (5.0, 0.0),          # m/s per km
    "wind": (2.0, 0.0),           # m/s
    "windDir": (255.0 / 360.0, 0.0),  # deg, direction the deck's mean wind blows TOWARD (shearDir's convention)
    "lapse": (20.0, 0.0),         # K/km
    "convCape": (0.25, 0.0),      # J/kg (CAPE_CON)
    "convBase": (20.0, 0.0),      # km MSL; 255 = none/missing
    "convTop": (20.0, 0.0),       # km MSL; 255 = none/missing
    "cruiseRhi": (200.0, 0.0),    # max RH(ice), fraction, 300-200 hPa
    "cruiseU": (1.0, 128.0),      # m/s at 250 hPa
    "cruiseV": (1.0, 128.0),
}
MISSING = 255
N_BYTES_V1 = 3 * len(DECK_CLUES) + len(NODE_CLUES)       # the ICX1 record: everything before the tail
N_BYTES = N_BYTES_V1 + 3 * len(DECK_TAIL_CLUES)

MOIST_RH = 90.0        # % over ice (below 0 C) or over water
# GUESS, like DECK_BANDS_KM: the air above the layer top that capTheta/capRh
# look at (and the air below it that sets the background gradient). Chosen by
# eye on synthetic columns; tune against real columns before trusting cap values.
CAP_WINDOW_M = 1500.0
SUBSTEPS = 20          # fine-grid points per pressure interval
CRUISE_P = (200.0, 300.0)
CRUISE_WIND_P = 250.0
WIND_DIR_MIN_MS = 1e-9  # a vector-mean wind slower than this (m/s) has no direction: windDir is missing


def rh_water_to_ice(rhw_pct, t_c):
    """RH over ice as a fraction, from RH over water in % at t_c degC."""
    t = np.asarray(t_c, dtype=float) + 273.15
    esw = 611.2 * np.exp(17.62 * (t - 273.15) / (t - 30.03))
    esi = np.exp(9.550426 - 5723.265 / t + 3.53068 * np.log(t) - 0.00728332 * t)
    return np.asarray(rhw_pct, dtype=float) / 100.0 * esw / esi


def _fine(profile):
    """Fine grid, linear in ln p (so gh is linear in ln p too). Thermodynamic
    fields (t, rh, gh) use only the levels where all three are valid; wind (u, v)
    uses only the levels where both are valid, so a missing wind never changes
    moisture or depth. Interpolation spans dropped levels; outside the valid
    range it is NaN. Returns equal-length arrays ordered from the ground up."""
    p = np.asarray(profile["p"], dtype=float)
    order = np.argsort(-p)  # descending pressure = ascending height
    p = p[order]
    fields = {k: np.asarray(profile[k], dtype=float)[order] for k in ("t", "rh", "gh", "u", "v")}
    x = -np.log(p)
    xs = [x[i] + np.linspace(0.0, 1.0, SUBSTEPS, endpoint=False) * (x[i + 1] - x[i])
          for i in range(len(p) - 1)]
    xs = np.concatenate(xs + [x[-1:]])
    th = np.isfinite(fields["t"]) & np.isfinite(fields["rh"]) & np.isfinite(fields["gh"])
    wd = np.isfinite(fields["u"]) & np.isfinite(fields["v"])
    g = {}
    for k in ("t", "rh", "gh", "u", "v"):
        m = th if k in ("t", "rh", "gh") else wd
        g[k] = (np.interp(xs, x[m], fields[k][m], left=np.nan, right=np.nan)
                if m.sum() >= 2 else np.full(len(xs), np.nan))
    g["p"] = np.exp(-xs)
    tk = g["t"] + 273.15
    g["theta"] = tk * (1000.0 / g["p"]) ** 0.2857
    rhi = rh_water_to_ice(g["rh"], g["t"]) * 100.0
    g["effrh"] = np.where(g["t"] < 0.0, rhi, g["rh"])  # % over ice below 0 C, else water
    return g


def _nan_deck():
    return {k: float("nan") for k in DECK_CLUES + DECK_TAIL_CLUES}


def _deck(g, lo_km, hi_km):
    out = _nan_deck()
    gh = g["gh"]
    ok = ~np.isnan(gh) & ~np.isnan(g["t"]) & ~np.isnan(g["effrh"])  # thermo only
    wok = ~np.isnan(g["u"]) & ~np.isnan(g["v"])
    valid_idx = np.nonzero(ok)[0]
    inband = ok & (gh >= lo_km * 1000.0) & (gh <= hi_km * 1000.0)
    if not inband.any():
        return out
    idx = np.nonzero(inband)[0]
    moist = inband & (g["effrh"] >= MOIST_RH)

    # Moist layer = the thickest unbroken run of moist fine points in the band
    # (a NaN or dry point breaks a run). Depth is edge point to edge point.
    best = None
    i = 0
    n = len(gh)
    while i < n:
        if moist[i]:
            j = i
            while j + 1 < n and moist[j + 1]:
                j += 1
            thick = gh[j] - gh[i]
            if best is None or thick > best[2]:
                best = (i, j, thick)
            i = j + 1
        else:
            i += 1

    if best is None:
        out["depth"] = 0.0
        seg = idx            # no layer: shear/wind/lapse describe the whole band
    else:
        a, b, _ = best
        # A run that touches the first or last valid point of the whole profile
        # could continue past what we can see: depth and cap are then unknown
        # (stay NaN = 255). A run that merely meets the deck-band edge with valid
        # data beyond it is reported at its in-band depth.
        truncated = a <= valid_idx[0] or b >= valid_idx[-1]
        seg = np.arange(a, b + 1)
        if len(seg) < 2:
            seg = idx
        if not truncated:
            out["depth"] = float(gh[b] - gh[a])
        # Cap: look up to CAP_WINDOW_M above the layer top, using any valid data
        # (the cap may sit above the band).
        layer_rh = min(100.0, float(np.max(g["effrh"][a:b + 1])))
        above = ok & (gh > gh[b]) & (gh <= gh[b] + CAP_WINDOW_M)
        if above.any() and not truncated:
            k = np.nonzero(above)[0]
            # Ordinary stability would add theta anyway, so the jump is what is
            # left after taking off the background gradient, measured over the
            # CAP_WINDOW_M of air just BELOW the layer (the layer's own mean when
            # there is no valid air below it).
            below = ok & (gh < gh[a]) & (gh >= gh[a] - CAP_WINDOW_M)
            if below.any() and gh[a] - gh[np.nonzero(below)[0][0]] >= 100.0:
                lo_i = np.nonzero(below)[0][0]
                grad = (g["theta"][a] - g["theta"][lo_i]) / (gh[a] - gh[lo_i])
            elif gh[b] - gh[a] >= 100.0:
                grad = (g["theta"][b] - g["theta"][a]) / (gh[b] - gh[a])
            else:
                grad = (g["theta"][idx[-1]] - g["theta"][idx[0]]) / max(gh[idx[-1]] - gh[idx[0]], 1.0)
            rise = g["theta"][k[-1]] - g["theta"][b]
            out["capTheta"] = float(max(0.0, rise - grad * (gh[k[-1]] - gh[b])))
            out["capRh"] = float(max(0.0, layer_rh - np.min(g["effrh"][k])))

    lo, hi = seg[0], seg[-1]
    dz = (gh[hi] - gh[lo]) / 1000.0
    if dz > 0:
        out["lapse"] = float((g["t"][lo] - g["t"][hi]) / dz)
    ws = seg[wok[seg]]
    if ws.size:
        out["wind"] = float(np.mean(np.hypot(g["u"][ws], g["v"][ws])))
        # windDir: where the VECTOR mean wind over the same points blows toward
        # (from north, clockwise); a calm mean has no direction.
        mu, mv = float(np.mean(g["u"][ws])), float(np.mean(g["v"][ws]))
        if math.hypot(mu, mv) >= WIND_DIR_MIN_MS:
            out["windDir"] = float(math.degrees(math.atan2(mu, mv)) % 360.0)
        wz = (gh[ws[-1]] - gh[ws[0]]) / 1000.0
        if wz > 0:
            du, dv = g["u"][ws[-1]] - g["u"][ws[0]], g["v"][ws[-1]] - g["v"][ws[0]]
            out["shear"] = float(math.hypot(du, dv) / wz)
            out["shearDir"] = float(math.degrees(math.atan2(du, dv)) % 360.0)  # toward, from north
    return out


def deck_clues(profile):
    g = _fine(profile)
    return {name: _deck(g, *DECK_BANDS_KM[name]) for name in DECK_NAMES}


def node_clues(profile):
    p = np.asarray(profile["p"], dtype=float)
    t = np.asarray(profile["t"], dtype=float)
    rh = np.asarray(profile["rh"], dtype=float)
    u = np.asarray(profile["u"], dtype=float)
    v = np.asarray(profile["v"], dtype=float)
    out = {k: float("nan") for k in NODE_CLUES}

    def single(key):
        x = profile.get(key)
        return float("nan") if x is None else float(x)

    out["convCape"] = single("cape_con")
    for key, name in (("hbas_con", "convBase"), ("htop_con", "convTop")):
        h = single(key)
        # -500 = the model has no convection here: missing, not a 0 km height.
        out[name] = float("nan") if (math.isnan(h) or h < 0) else h / 1000.0

    cr = (p >= CRUISE_P[0]) & (p <= CRUISE_P[1])
    rhi = rh_water_to_ice(rh[cr], t[cr])
    rhi = rhi[~np.isnan(rhi)]
    if rhi.size:
        out["cruiseRhi"] = float(rhi.max())
    k = int(np.argmin(np.abs(p - CRUISE_WIND_P)))
    if abs(p[k] - CRUISE_WIND_P) <= 25.0:
        out["cruiseU"] = float(u[k])
        out["cruiseV"] = float(v[k])
    return out


def _byte(name, val):
    if val is None or not np.isfinite(val):
        return MISSING
    scale, off = CLUE_LAYOUT[name]
    return int(min(254, max(0, math.floor(float(val) * scale + off + 0.5))))


def encode_clues(deck, node):
    b = []
    for d in DECK_NAMES:
        b += [_byte(c, deck[d][c]) for c in DECK_CLUES]
    b += [_byte(c, node[c]) for c in NODE_CLUES]
    for d in DECK_NAMES:
        b += [_byte(c, deck[d][c]) for c in DECK_TAIL_CLUES]
    assert len(b) == N_BYTES
    return bytes(b)


def decode_clues(data):
    """Inverse of encode_clues (to byte precision); 255 -> None. A 27-byte
    record (ICX1) has no tail: its windDir is None."""
    def one(name, byte):
        if byte == MISSING:
            return None
        scale, off = CLUE_LAYOUT[name]
        return (byte - off) / scale
    deck, n = {}, 0
    for d in DECK_NAMES:
        deck[d] = {c: one(c, data[n + i]) for i, c in enumerate(DECK_CLUES)}
        n += len(DECK_CLUES)
    node = {c: one(c, data[n + i]) for i, c in enumerate(NODE_CLUES)}
    n += len(NODE_CLUES)
    tail = len(data) >= N_BYTES
    for d in DECK_NAMES:
        for c in DECK_TAIL_CLUES:
            deck[d][c] = one(c, data[n]) if tail else None
            n += 1
    return {"decks": deck, "node": node}
