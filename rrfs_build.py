#!/usr/bin/env python3
# rrfs_build.py — newest complete NOAA RRFS run -> site-rrfs (ICL1 + manifest).
# Exit codes mirror hrrr_build.py: 0 built, 3 nothing to do (the live manifest
# already carries the newest complete run), 1 failed (the live site stays).
# Every request to NOMADS goes through one RateLimiter (under 100 a minute).
import argparse
import datetime as dt
import json
import sys
import time

import hrrr_build
from hrrr_build import BUILT, FAILED, NOTHING_TO_DO, parse_hours
from rrfs import build as rb

LIMITER = rb.RateLimiter(rb.REQUESTS_PER_MINUTE)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _exists_raw(url):
    return hrrr_build._exists(url)


def _get_raw(url, rng=None):
    return hrrr_build._get(url, rng)


def _exists(url):
    LIMITER.acquire()
    return _exists_raw(url)


def _get(url, rng=None):
    return rb.limited(_get_raw, LIMITER)(url, rng)


def _http_text(url):
    return hrrr_build._http_text(url)


def _sleep(s):
    time.sleep(s)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site-rrfs")
    p.add_argument("--live-manifest")
    p.add_argument("--hours", help="only these forecast hours, e.g. 0,3,6-9 (a sample run; its manifest says complete: false)")
    a = p.parse_args(argv)
    try:
        now = _now()
        run = rb.newest_complete_run(_exists, now)
        if run is None:
            print("no complete RRFS run in the last 4 cycles")
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
        names = rb.build(run, a.out, _get, now, sleep=_sleep, fhs=parse_hours(a.hours))
        print(f"built {iso}: {len(names)} squares")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
