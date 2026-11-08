#!/usr/bin/env python3
# places_build.py — GeoNames dump -> site-places (the app's place search index).
# Exit codes mirror aqm_build.py: 0 built, 3 nothing to do (the live manifest is
# under 25 days old; --force overrides), 1 failed (the live index stays).
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from places import build as pb

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1
DUMP = "https://download.geonames.org/export/dump/"
FRESH_DAYS = 25


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _http_text(url, attempts=4):
    for n in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read().decode("utf-8")
        except Exception:
            if n == attempts - 1:
                break
            time.sleep(2 * (n + 1))
    # urllib kept failing (a flaky proxy truncates reads): let curl try, once
    r = subprocess.run(["curl", "-fsS", "--retry", "3", "-m", "120", url],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not fetch {url}: {r.stderr.decode()[:200]}")
    return r.stdout.decode("utf-8")


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def live_age_days(version, now):
    try:
        d = dt.datetime.strptime(str(version), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (now - d).total_seconds() / 86400


def main(argv=None, fetch=_http_text, download=_download, now=_now):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site-places")
    p.add_argument("--live-manifest")
    p.add_argument("--zip", help="a local allCountries.zip (otherwise downloaded)")
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    try:
        t = now()
        if a.live_manifest and not a.force:
            try:
                age = live_age_days(json.loads(fetch(a.live_manifest)).get("version"), t)
            except Exception:
                age = None      # no live manifest yet: build
            if age is not None and age < FRESH_DAYS:
                print(f"live index is {age:.0f} days old (under {FRESH_DAYS}); nothing to do")
                return NOTHING_TO_DO
        admin1 = fetch(DUMP + "admin1CodesASCII.txt")
        countries = fetch(DUMP + "countryInfo.txt")
        out = Path(a.out)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        tmp = Path(tempfile.mkdtemp(prefix="places-", dir=str(out.parent.resolve())))
        try:
            zip_path = a.zip
            if not zip_path:
                zip_path = str(tmp / "allCountries.zip")
                download(DUMP + "allCountries.zip", zip_path)
            m = pb.build(zip_path, out, admin1, countries, t, tmp_dir=tmp / "work")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        s = m["_stats"]
        print(f"built {m['version']}: {s['places']:,} places, {s['files']:,} files, "
              f"{s['bytes']:,} bytes, {s['split']:,} split keys, {s['exact']:,} exact keys, built in {s['duration_ms']} ms")
        print(f"biggest file: {s['biggest'][1]!r} {s['biggest'][0]:,} bytes; "
              f"over the cap: {s['oversize']}")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        shutil.rmtree(a.out, ignore_errors=True)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
