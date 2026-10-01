# pipeline/dwd.py — where a DWD ICON global run lives and whether it is whole.
#
# DWD publishes each run under grib/<HH>/<field>/, one directory per run hour
# of the day; the run DATE is only in the file names. A run uploads over ~20
# minutes, so "newest" is not enough: a build must only take a run whose
# every file we need is listed, for all four fields.
import datetime as dt
import re

BASE = "https://opendata.dwd.de/weather/nwp/icon/grib"
RUN_HOURS = ["00", "06", "12", "18"]
FIELDS = ["CLCL", "CLCM", "CLCH", "CLCT"]          # low, mid, high, total — the file's field order
# Hourly to 78 h, then every 3 h to 120 h: enough to reach the far end of the
# app's window (today+2, 23:59 local, any zone) from a run up to 9 h old.
STEPS = list(range(0, 79)) + list(range(81, 121, 3))

# The tail, for days 4-5 in the app (Premium's 5-day forecast): hours 123-144
# of the newest whole 00Z or 12Z run. Only those two runs go past 120 h
# (verified on DWD's listing 2026-09-30: 00/12 reach 180, 06/18 stop at 120).
TAIL_RUN_HOURS = ["00", "12"]
TAIL_STEPS = list(range(123, 145, 3))

_NAME = re.compile(r"icon_global_icosahedral_single-level_(\d{10})_(\d{3})_([A-Z]+)\.grib2\.bz2")


def run_id(run):
    return run.strftime("%Y%m%d%H")


def run_iso(run):
    return run.strftime("%Y-%m-%dT%HZ")


def file_name(run, step, field):
    return f"icon_global_icosahedral_single-level_{run_id(run)}_{step:03d}_{field}.grib2.bz2"


def file_url(run, step, field):
    return f"{BASE}/{run:%H}/{field.lower()}/{file_name(run, step, field)}"


def fr_land_url(run):
    """DWD's invariant land fraction, published beside every run."""
    return f"{BASE}/{run:%H}/fr_land/icon_global_icosahedral_time-invariant_{run_id(run)}_FR_LAND.grib2.bz2"


def listing_url(hh, field):
    return f"{BASE}/{hh}/{field.lower()}/"


def run_from_listing(html):
    """The run time the listed files belong to, or None for an empty listing."""
    m = _NAME.search(html or "")
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)


def missing_files(html, run, field, steps=None):
    steps = STEPS if steps is None else steps
    present = set(_NAME.findall(html or ""))
    return [file_name(run, s, field) for s in steps
            if (run_id(run), f"{s:03d}", field) not in present]


def newest_complete_run(listing, run_hours=None, steps=None):
    """`listing(hh, field) -> html`. The newest run (by time) whose every
    needed file is listed for every field, else None. Defaults: the near
    product's run hours and steps."""
    candidates = []
    for hh in (RUN_HOURS if run_hours is None else run_hours):
        html = listing(hh, FIELDS[0])
        run = run_from_listing(html)
        if run is None:
            continue
        if all(not missing_files(html if f == FIELDS[0] else listing(hh, f), run, f, steps) for f in FIELDS):
            candidates.append(run)
    return max(candidates) if candidates else None
