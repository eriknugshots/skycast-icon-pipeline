# places/build.py — GeoNames allCountries.zip -> a prefix-trie search index.
#
# Output (the contract the app reads):
#   manifest.json                         written LAST
#   tiles/<version>/<key_file(k)>.tsv     rows: key, name, lat, lon, feature,
#                                         country_code, country, admin1,
#                                         population, elevation, timezone;
#                                         best first
# manifest = {version, runMs, builtMs, complete: true, tiles, split, attribution}.
# `split` lists every key whose rows did not fit one file. A split key's own file
# holds the TOP_N best rows of its subtree (plus its TOP_N best rows whose key is
# exactly that key); its children extend the key by one character. A reader takes
# the shortest prefix of the query that is NOT in `split` and opens that file.
#
# Every GeoNames row is kept, of every class, at any population, in any script.
# Each place yields: a row for its name; a row per alternate name (name shown =
# the alternate); alias rows for a leading mount/saint/fort/point word
# (mount<->mt, saint<->st, fort<->ft, point<->pt; name shown = the place's name);
# and a row per later word of the name that is 3+ letters (name shown = the
# place's name). Duplicate keys on one place collapse, the first kind wins.
#
# Memory: rows stream out of the zip into one temp file per first key character;
# an oversize file is split on disk by the next character. Only a bucket that
# fits one shard is ever sorted in memory.
import datetime as dt
import heapq
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from .normalize import key_file, norm

MAX_SHARD_BYTES = 128 * 1024
TOP_N = 200
MIN_ROWS = 10_000_000          # fewer places than this = a truncated download
ATTRIBUTION = "Places: GeoNames (CC BY 4.0)"
MAX_PREFIX = 30                # a key is never split past this many characters
MAX_STEM = 180                 # nor past this many characters of file name
FLUSH_BYTES = 192 * 1024 * 1024

RANK_BASE = 10 ** 15           # stored rank = RANK_BASE - score, zero padded
RANK_W = 16
PREFIX_LEN = RANK_W + 1        # the rank column and its tab

# What a feature is worth when it has no population to rank by. A row ranks by
# max(population, floor): capitals and countries first, then admin seats, then
# named mountains, lakes and islands above a populated-place with population 0.
FLOOR = {"PPLC": 2_000_000_000, "PCLI": 2_000_000_000,
         "PCLD": 100_000_000, "PCLF": 100_000_000, "PCLS": 100_000_000,
         "PCLH": 100_000_000, "PCL": 100_000_000, "TERR": 100_000_000,
         "ADM1": 1_000_000, "PPLA": 500_000, "PPLA2": 100_000, "ADM2": 50_000,
         "PPLA3": 20_000, "PPLA4": 20_000, "PPLA5": 20_000, "ADM3": 20_000,
         "ADM4": 20_000, "ADM5": 20_000, "ADMD": 20_000, "PPLG": 20_000}
for _c in ("MT", "MTS", "PK", "PKS", "VLC", "ISL", "ISLS", "LK", "LKS", "PRK",
           "NTNL", "RSV", "CAPE", "BCH", "DSRT", "VAL", "FLLS", "GLCR", "RGN"):
    FLOOR[_c] = 3_000
WORD_DIVISOR = 50              # a word row ranks as a fiftieth of its place

ALIASES = {"mount": "mt", "mt": "mount", "saint": "st", "st": "saint",
           "fort": "ft", "ft": "fort", "point": "pt", "pt": "point"}
_WORDS = re.compile(r"[\W_]+")
_SPACE = re.compile(r"[\t\r\n]+")


class BuildRefused(Exception):
    """The input is not a whole GeoNames dump; the live index stays."""


def parse_admin1(text):
    out = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) >= 2 and p[0]:
            out[p[0]] = p[1]
    return out


def parse_countries(text):
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 5:
            out[p[0]] = p[4]
    return out


def _clean(s):
    return _SPACE.sub(" ", s).strip()


def rank_of(feature, population, kind):
    """kind: 3 the place's own name, 2 an alternate or alias, 1 a word.
    Returns the stored (inverted, zero padded) rank: bytes sort best first."""
    score = max(population, FLOOR.get(feature, 0))
    if kind == 1:
        score //= WORD_DIVISOR
    return "%0*d" % (RANK_W, RANK_BASE - (score * 4 + kind))


def place_rows(name, alternates, tail, feature, population):
    """Yield (key, rank, line-without-rank) for one place. `tail` is the
    tab-joined lat..timezone columns after the name."""
    seen = set()
    shown = _clean(name)
    pk = norm(name)
    if pk:
        seen.add(pk)
        yield pk, rank_of(feature, population, 3), f"{pk}\t{shown}\t{tail}"
    if alternates:
        for alt in alternates.split(","):
            alt = _clean(alt)
            k = norm(alt) if alt else ""
            if k and k not in seen:
                seen.add(k)
                yield k, rank_of(feature, population, 2), f"{k}\t{alt}\t{tail}"
    words = [w for w in _WORDS.split(name) if w]
    if len(words) < 2:
        return
    first = norm(words[0])
    if first in ALIASES:
        k = ALIASES[first] + norm("".join(words[1:]))
        if k not in seen:
            seen.add(k)
            yield k, rank_of(feature, population, 2), f"{k}\t{shown}\t{tail}"
    for w in words[1:]:
        k = norm(w)
        if len(k) >= 3 and k not in seen:
            seen.add(k)
            yield k, rank_of(feature, population, 1), f"{k}\t{shown}\t{tail}"


def _stem(prefix):
    return "-".join("%x" % ord(c) for c in prefix)


class Buckets:
    """Append-only temp files, one per key prefix, buffered in memory."""

    def __init__(self, directory, flush_bytes=FLUSH_BYTES):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_bytes = flush_bytes
        self.buf = {}
        self.held = 0
        self.size = {}        # prefix -> bytes the final shard rows will take
        self.count = {}

    def path(self, prefix):
        return self.dir / (_stem(prefix) + ".b")

    def add(self, prefix, rank, line):
        self.add_raw(prefix, (rank + "\t" + line + "\n").encode("utf-8"))

    def add_raw(self, prefix, b):
        self.buf.setdefault(prefix, []).append(b)
        self.held += len(b)
        self.size[prefix] = self.size.get(prefix, 0) + len(b) - PREFIX_LEN
        self.count[prefix] = self.count.get(prefix, 0) + 1
        if self.held >= self.flush_bytes:
            self.flush()

    def flush(self):
        for prefix, lines in self.buf.items():
            with open(self.path(prefix), "ab") as f:
                f.write(b"".join(lines))
        self.buf = {}
        self.held = 0


def _can_extend(prefix):
    return len(prefix) < MAX_PREFIX and len(key_file(prefix)) < MAX_STEM


class _Writer:
    def __init__(self, tiles, max_shard, top_n, tmp):
        self.tiles = tiles
        self.max_shard = max_shard
        self.top_n = top_n
        self.tmp = Path(tmp)
        self.split = []
        self.files = 0
        self.bytes = 0
        self.biggest = (0, "")
        self.oversize = []     # (bytes, key) of files over max_shard

    def emit(self, prefix, lines):
        data = b"".join(l[PREFIX_LEN:] + b"\n" for l in lines)
        stem = key_file(prefix)
        (self.tiles / (stem + ".tsv")).write_bytes(data)
        self.files += 1
        self.bytes += len(data)
        if len(data) > self.biggest[0]:
            self.biggest = (len(data), prefix)
        if len(data) > self.max_shard:
            self.oversize.append((len(data), prefix))

    def leaf(self, path):
        lines = path.read_bytes().split(b"\n")[:-1]
        lines.sort()
        return lines

    def process(self, prefix, size, bk_dir):
        path = Path(bk_dir) / (_stem(prefix) + ".b")
        if size <= self.max_shard or not _can_extend(prefix):
            self.emit(prefix, self.leaf(path))
            path.unlink()
            return
        n = len(prefix)
        child = Buckets(self.tmp / ("c" + _stem(prefix)))
        top, exact = [], []
        keep = self.top_n * 8
        with open(path, "rb") as f:
            for line in f:
                line = line.rstrip(b"\n")
                key = line[PREFIX_LEN:].split(b"\t", 1)[0].decode("utf-8")
                top.append(line)
                if len(top) > keep:
                    top = heapq.nsmallest(self.top_n, top)
                if len(key) > n:
                    child.add_raw(key[: n + 1], line + b"\n")
                else:
                    exact.append(line)
                    if len(exact) > keep:
                        exact = heapq.nsmallest(self.top_n, exact)
        child.flush()
        if not child.size:
            # every row is exactly this key: it cannot split further
            shutil.rmtree(child.dir, ignore_errors=True)
            self.emit(prefix, self.leaf(path))
            path.unlink()
            return
        best = sorted(set(heapq.nsmallest(self.top_n, top) + heapq.nsmallest(self.top_n, exact)))
        self.split.append(prefix)
        self.emit(prefix, best)
        path.unlink()
        for cp in sorted(child.size):
            self.process(cp, child.size[cp], child.dir)
        shutil.rmtree(child.dir, ignore_errors=True)


def build(zip_path, out_dir, admin1_text, countries_text, now=None,
          tmp_dir=None, max_shard_bytes=MAX_SHARD_BYTES, top_n=TOP_N,
          min_rows=MIN_ROWS, log=print):
    """Build the index into out_dir. Returns the manifest. Raises BuildRefused
    when the dump has fewer than min_rows places (nothing is written)."""
    t0 = time.time()
    now = now or dt.datetime.now(dt.timezone.utc)
    version = now.strftime("%Y%m%d")
    out = Path(out_dir)
    admin1 = parse_admin1(admin1_text)
    countries = parse_countries(countries_text)
    tmp = Path(tmp_dir) if tmp_dir else out.parent / (out.name + "-tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    root = Buckets(tmp / "root")
    places = skipped = 0
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt") and "readme" not in n.lower())
        with z.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="\n"):
                p = line.rstrip("\n").split("\t")
                if len(p) < 18:
                    continue
                places += 1
                feature = p[7]
                try:
                    pop = int(p[14]) if p[14] else 0
                except ValueError:
                    pop = 0
                elev = p[15] if p[15] not in ("", "-9999") else (p[16] if p[16] != "-9999" else "")
                if elev == "-9999":
                    elev = ""
                cc = p[8]
                tail = "\t".join((p[4], p[5], feature, cc, _clean(countries.get(cc, "")),
                                  _clean(admin1.get(f"{cc}.{p[10]}", "")) if p[10] else "",
                                  str(pop) if p[14] else "", elev, p[17]))
                made = False
                for key, rank, row in place_rows(p[1], p[3], tail, feature, pop):
                    root.add(key[0], rank, row)
                    made = True
                if not made:
                    skipped += 1
                if places % 2_000_000 == 0:
                    log(f"  {places:,} places read ({time.time() - t0:.0f}s)")
    root.flush()
    log(f"read {places:,} places; {skipped:,} had no searchable name")
    if places < min_rows:
        shutil.rmtree(tmp, ignore_errors=True)
        raise BuildRefused(f"only {places} places in the dump (need {min_rows})")
    tiles = out / "tiles" / version
    if tiles.exists():
        shutil.rmtree(tiles)
    tiles.mkdir(parents=True)
    w = _Writer(tiles, max_shard_bytes, top_n, tmp)
    for prefix in sorted(root.size):
        w.process(prefix, root.size[prefix], root.dir)
    shutil.rmtree(tmp, ignore_errors=True)
    run_ms = int((time.time() - t0) * 1000)
    manifest = {
        "version": version,
        "runMs": run_ms,
        "builtMs": int(time.time() * 1000),
        "complete": True,
        "tiles": f"tiles/{version}",
        "split": sorted(w.split),
        "attribution": ATTRIBUTION,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    manifest["_stats"] = {"places": places, "skipped": skipped, "files": w.files,
                          "bytes": w.bytes, "biggest": w.biggest,
                          "oversize": sorted(w.oversize, reverse=True)[:10]}
    return manifest
