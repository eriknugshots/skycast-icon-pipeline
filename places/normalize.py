# places/normalize.py — the search key of a place name, and the file a key lives in.
# The app keeps an identical norm() (JavaScript, src/placesearch.js in the app repo) and pins it with the same vectors
# file (tests/fixtures/place_norm_vectors.json). Change one, change both.
import re
import unicodedata

_ASCII_DROP = re.compile(r"[^a-z0-9]")
_MAP = str.maketrans({
    "ß": "ss", "ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe",
    "þ": "th", "ð": "d", "ı": "i",
})
_CACHE = {}
_CACHE_MAX = 400_000


def norm(s):
    """Lowercase, fold the letters NFKD cannot (ß ø ł đ æ œ þ ð ı), decompose,
    drop combining marks, keep only letters and digits (any script)."""
    if s.isascii():
        return _ASCII_DROP.sub("", s.lower())
    hit = _CACHE.get(s)
    if hit is not None:
        return hit
    # NFKD splits letters from their accents; the final filter keeps only
    # categories L* and N*, which is the step that drops EVERY combining mark
    # (Mn, Mc, Me: accents and Indic vowel signs alike). The JS copy does the
    # same with /[\p{L}\p{N}]/u after normalize("NFKD"); no separate mark strip.
    t = unicodedata.normalize("NFKD", s.lower().translate(_MAP))
    out = "".join(c for c in t if unicodedata.category(c)[0] in "LN")
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[s] = out
    return out


def key_file(key):
    """File stem for a key: ASCII letters and digits stay, anything else is
    'x' plus its code point in lowercase hex."""
    return "".join(
        c if (c.isascii() and c.isalnum()) else "x%x" % ord(c) for c in key
    )
