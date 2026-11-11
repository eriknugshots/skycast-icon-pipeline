#!/usr/bin/env python3
# hrrr_fog_build.py — the live HRRR run's 3 km fog tiles -> site-hrrr-fog.
# Reads the live manifest, builds fog for its run, writes manifest.json = the
# live manifest unchanged + "fog": entry. Exit codes: 0 built, 3 nothing to do
# (live already carries fog for its run, or the live run moved while building),
# 1 failed (the live site stays).
import argparse
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

from hrrr import fogtiles
from hrrr.build import RETRIES, last_hour

BUILT, NOTHING_TO_DO, FAILED = 0, 3, 1


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _get(url, rng=None, opener=urllib.request.urlopen):
    """A short-but-200 body (a proxy truncating) raises so the retry wrapper retries it."""
    headers = {}
    if rng:
        headers["Range"] = f"bytes={rng[0]}-{'' if rng[1] is None else rng[1]}"
    with opener(urllib.request.Request(url, headers=headers), timeout=120) as r:
        body = r.read()
        want = None
        if rng and rng[1] is not None:
            want = rng[1] - rng[0] + 1
        else:
            cl = r.headers.get("Content-Length") if getattr(r, "headers", None) else None
            if cl is not None:
                want = int(cl)
    if want is not None and len(body) != want:
        raise IOError(f"short read from {url}: {len(body)} of {want} bytes")
    return body


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def parse_run(text):
    return dt.datetime.strptime(text, "%Y-%m-%dT%HZ").replace(tzinfo=dt.timezone.utc)


def _retrying(get, sleep):
    """Every S3 read gets hrrr.build's RETRIES, so one hiccup does not abort a 49-hour build."""
    def wrapped(url, rng=None):
        for attempt in range(RETRIES + 1):
            try:
                return get(url, rng)
            except Exception:
                if attempt == RETRIES:
                    raise
                sleep(2 * (attempt + 1))
    return wrapped


def main(argv=None, fetch_json=_fetch_json, get=_get, now=_now, sleep=time.sleep,
         build=fogtiles.build_fog):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site-hrrr-fog")
    p.add_argument("--live-manifest", required=False)
    a = p.parse_args(argv)
    try:
        if not a.live_manifest:
            print("FAILED: --live-manifest is required")
            return FAILED
        t0 = time.monotonic()
        live = fetch_json(a.live_manifest)
        iso = live["run"]
        if (live.get("fog") or {}).get("run") == iso:
            print(f"live site already carries fog for {iso}")
            return NOTHING_TO_DO
        run = parse_run(iso)
        out = Path(a.out)
        entry = build(run, out, _retrying(get, sleep), list(range(last_hour(run) + 1)))
        again = fetch_json(a.live_manifest)
        if again.get("run") != iso:
            print(f"live run moved {iso} -> {again.get('run')} while building; not publishing")
            return NOTHING_TO_DO
        manifest = dict(again)
        manifest["fog"] = entry
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
        files = [f for f in (out / "fog").rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        print(f"built fog {iso}: {len(entry['tileNames'])} tiles, {len(files)} files, "
              f"{size / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
