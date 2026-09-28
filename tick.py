#!/usr/bin/env python3
# tick.py — is there a DWD run the site does not carry yet? Exit 0 = yes (start
# a build), 3 = no. Stdlib only: the tick workflow calls it every 10 minutes
# for hours, and GitHub's own schedule for build.yml fired only every 3-6 h
# (2026-09-27/28), which let the live run age past the app's 12 h limit.
import argparse
import sys
import urllib.request

from pipeline import dwd
from pipeline.live import live_complete_run

NEEDED, NOTHING = 0, 3


def needs_build(listing, fetch_text, pages_base):
    run = dwd.newest_complete_run(listing)
    if run is None:
        return False
    return live_complete_run(fetch_text, pages_base) != dwd.run_iso(run)


def _http_text(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages-base", required=True)
    a = p.parse_args()
    need = needs_build(lambda hh, field: _http_text(dwd.listing_url(hh, field)), _http_text, a.pages_base)
    print("new run on DWD — build" if need else "site is current")
    sys.exit(NEEDED if need else NOTHING)


if __name__ == "__main__":
    main()
