# pipeline/thick.py — the thickness byte: cloud ice / water column amount as one
# log-scaled u8. TQI and TQC arrive in kg/m²; the byte works in g/m² because
# the thin cirrus that matters sits near 1 g/m² and thick cumulonimbus near
# several kg/m², a range only a log scale can hold in 254 steps.
#
#   0    under W_MIN (nothing worth drawing; also zero and negative noise)
#   255  missing (no data, or NaN)
#   1..254  1 + round(253 · ln(W / W_MIN) / ln(W_MAX / W_MIN)), W in g/m²
#
# Anything above W_MAX clips to 254.
import math
import numpy as np

W_MIN_G = 1.0
W_MAX_G = 5000.0
MISSING = 255
_SPAN = math.log(W_MAX_G / W_MIN_G)


def thick_bytes(kg_m2):
    kg = np.asarray(kg_m2, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        w = kg * 1000.0
        # The 1e-9 g slack keeps 0.001 kg/m² (exactly W_MIN) from falling a
        # float-rounding hair under the floor and reading as 0.
        under = w < W_MIN_G * (1 - 1e-9)
        pos = np.log(np.where(under | np.isnan(w), W_MIN_G, w) / W_MIN_G)
        b = 1 + np.floor(253 * pos / _SPAN + 0.5)
    b = np.clip(b, 1, 254)
    b = np.where(under, 0, b)
    b = np.where(np.isnan(kg), MISSING, b)
    return b.astype(np.uint8)


def grams_of_byte(b):
    """The amount a byte stands for (the centre of its step); 0 for 0, nan for 255."""
    b = int(b)
    if b == 0:
        return 0.0
    if b == MISSING:
        return math.nan
    # pow, not exp(log): byte 254 comes back as exactly W_MAX_G.
    return W_MIN_G * (W_MAX_G / W_MIN_G) ** ((b - 1) / 253)
