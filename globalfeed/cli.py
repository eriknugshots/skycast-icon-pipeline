# globalfeed/cli.py — the command line shared by gfs_build.py and aifs_build.py.
import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request

from hrrr_build import _exists, _get, _http_text, BUILT, NOTHING_TO_DO, FAILED

from . import build as gb


def _now():
    return dt.datetime.now(dt.timezone.utc)


def main(model, argv=None, now=None, exists=_exists, get=_get, http_text=_http_text, sleep=time.sleep):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=f"site-{model.name}")
    p.add_argument("--state", default=f"state-{model.name}")
    p.add_argument("--live-manifest")
    a = p.parse_args(argv)
    try:
        now = now or _now()
        run = gb.newest_complete_run(model, exists, now)
        if run is None:
            print(f"no complete {model.name} run in the last 4 cycles")
            return FAILED
        iso = run.strftime("%Y-%m-%dT%HZ")
        if a.live_manifest:
            try:
                live = json.loads(http_text(a.live_manifest)).get("run")
            except Exception:
                live = None
            if live == iso:
                print(f"live site already carries {iso}")
                return NOTHING_TO_DO
        names = gb.build(model, run, a.out, get, now, a.state, sleep=sleep)
        print(f"built {iso}: {len(names)} squares")
        return BUILT
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return FAILED
