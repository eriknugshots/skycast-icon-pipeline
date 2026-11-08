#!/usr/bin/env python3
# aqm_build.py — newest complete NOAA AQM cycle -> site-aqm (PM2.5 squares + manifest).
# Exit codes mirror hrrr_build.py: 0 built, 3 nothing to do (the live manifest
# already carries the newest complete run), 1 failed (the live site stays).
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

from aqm import build as ab
from aqm.grib import file_url, read_hours
from pipeline.site import run_iso

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _exists(run):
    req = urllib.request.Request(file_url(run), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False
        raise


def _read(run):
    """Download one cycle's file, decode it, delete it. None when unpublished."""
    req = urllib.request.Request(file_url(run))
    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    return None
                raise
        return read_hours(path)
    finally:
        os.unlink(path)


def _http_text(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode()


def main(argv=None, read=_read, exists=_exists):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site-aqm")
    p.add_argument("--live-manifest")
    a = p.parse_args(argv)
    try:
        now = _now()
        run = ab.newest_complete_run(exists, now)
        if run is None:
            print("no published AQM cycle in the last 4")
            return FAILED
        iso = run_iso(run)
        if a.live_manifest:
            try:
                live = json.loads(_http_text(a.live_manifest)).get("run")
            except Exception:
                live = None      # no live manifest yet: build
            if live == iso:
                print(f"live site already carries {iso}")
                return NOTHING_TO_DO
        names = ab.build(run, a.out, read, now)
        print(f"built {iso}: {len(names)} squares")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
