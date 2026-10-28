# pipeline/live.py — what the published feed carries (stdlib only, so the
# tick watcher can ask without installing numpy or cdo).
import json
import os
import time
import urllib.error
import urllib.request

# The Vercel Blob store the app reads (SkyCast src/icondirect.js
# DIRECT_BLOB_BASE). Its manifest is the one that counts: Pages' copy never
# names the column feed, the thickness or the day blocks, and a Blob upload
# that failed after Pages went out left the app on an old run while Pages
# looked current.
BLOB_BASE = os.environ.get("ICON_BLOB_BASE", "https://kq0vzbgeghiuc2ov.public.blob.vercel-storage.com")

# A run whose Blob manifest lacks one of these was published with a piece
# missing (2026-10-28: a dropped connection cost the 18Z run its fog columns,
# and nothing rebuilt it). The next tick rebuilds it whole, at most
# MAX_REPAIRS times — DWD may simply not have the fields.
FAMILIES = ("columns", "thick", "blocks", "tail")
MAX_REPAIRS = 2


def http_text(url, timeout=60, attempts=3, urlopen=urllib.request.urlopen, sleep=time.sleep):
    """GET url as text, retrying a dropped connection, a reset, a timeout or a
    5xx after a short pause. A 4xx is raised at once: retrying will not help."""
    last = None
    for i in range(attempts):
        if i:
            sleep(2 * i)
        try:
            with urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except OSError as e:          # URLError, RemoteDisconnected, resets, timeouts
            last = e
        except Exception as e:        # http.client.HTTPException (IncompleteRead, BadStatusLine)
            last = e
    raise last


def _manifest(fetch_text, base):
    if not base:
        return None
    try:
        return json.loads(fetch_text(base.rstrip("/") + "/manifest.json"))
    except Exception:
        return None


def live_status(fetch_text, pages_base, blob_base=BLOB_BASE):
    """(run iso or None, whole, repairs) for the live feed. Read from Blob,
    where the app reads; whole = complete AND every family present. Falls back
    to Pages' manifest (whole = complete) when Blob cannot be read, so a Blob
    outage cannot set builds looping."""
    m = _manifest(fetch_text, blob_base)
    if m is not None:
        whole = bool(m.get("complete")) and all(m.get(f) for f in FAMILIES)
        return m.get("run"), whole, int(m.get("repairs") or 0)
    m = _manifest(fetch_text, pages_base)
    if m is not None:
        return m.get("run"), bool(m.get("complete")), 0
    return None, False, 0


def run_is_done(fetch_text, pages_base, run_iso, blob_base=BLOB_BASE):
    """True when `run_iso` needs no (re)build: live and whole, or already
    rebuilt MAX_REPAIRS times. A test build (some steps, some squares)
    publishes the same id with complete=false and never counts."""
    run, whole, repairs = live_status(fetch_text, pages_base, blob_base)
    return run == run_iso and (whole or repairs >= MAX_REPAIRS)


def repairs_for(fetch_text, pages_base, run_iso, blob_base=BLOB_BASE):
    """The repair count a new build of `run_iso` should carry: one more than
    the live manifest's when it is the same run, else 0."""
    run, _, repairs = live_status(fetch_text, pages_base, blob_base)
    return repairs + 1 if run == run_iso else 0


def live_complete_run(fetch_text, pages_base, blob_base=BLOB_BASE):
    """The run the live feed carries COMPLETELY, as its iso string, or None."""
    run, whole, _ = live_status(fetch_text, pages_base, blob_base)
    return run if whole else None
