#!/usr/bin/env python3
# hrrr_build.py — newest complete NOAA HRRR run -> site-hrrr (ICL1 + manifest).
# Exit codes mirror run_build.py: 0 built, 3 nothing to do (the live manifest
# already carries the newest complete run), 1 failed (the live site stays).
import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

from hrrr import build as hb

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _exists(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False
        raise


def _get(url, rng=None):
    headers = {}
    if rng:
        headers["Range"] = f"bytes={rng[0]}-{'' if rng[1] is None else rng[1]}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        return r.read()


def _http_text(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode()


def _sleep(s):
    time.sleep(s)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site-hrrr")
    p.add_argument("--live-manifest")
    a = p.parse_args(argv)
    try:
        now = _now()
        run = hb.newest_complete_run(_exists, now)
        if run is None:
            print("no complete HRRR run in the last 4 cycles")
            return FAILED
        iso = run.strftime("%Y-%m-%dT%HZ")
        if a.live_manifest:
            try:
                live = json.loads(_http_text(a.live_manifest)).get("run")
            except Exception:
                live = None      # no live manifest yet: build
            if live == iso:
                print(f"live site already carries {iso}")
                return NOTHING_TO_DO
        names = hb.build(run, a.out, _get, now, sleep=_sleep)
        print(f"built {iso}: {len(names)} squares")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
