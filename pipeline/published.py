# pipeline/published.py — when each run went out, for the runs kept as history.
#
# WHY. A live manifest says when its own run was built (builtMs), but the
# history ahead of it — earlier runs' heads (history.py, globalfeed/history6.py)
# — carried no time at all, so a reader asking "was this run out when the photo
# was taken?" (SkyCast's report fill) had to guess from a measured lag. Erik's
# rule is never to make data up; this keeps the real time instead.
#
# WHAT. state/published.json = {"<YYYYMMDDHH>": publishedMs, ...}: the moment
# the workflow's Blob publish of that run RETURNED (manifest.json up, prune
# done), recorded just before the state push — so it is never earlier than
# the manifest went live. builtMs is earlier still (stamped before the upload;
# for GFS/AIFS/IFS at the build's start), which is why this is not builtMs.
# A run published twice keeps its LATEST time: a repair rebuild (live.py) is a
# run that first went out without its columns or thickness, and the record is
# per run, not per part — the later time is the first moment ALL of it was out.
# Erring late costs a few fills; erring early would admit data nobody had.
#
# The next build lists the recorded runs older than its own in its manifest as
# historyRuns: [{runMs, publishedMs}, ...] ascending. Readers that predate the
# key ignore it; a run with no entry (published before this file existed, or a
# lost state) is simply absent.
#
# SINCE. The file also keeps "since": the time of the first record it holds
# (set by the first record() that finds none, so for a file that predates the
# key it is that later moment — never earlier than the truth). The manifest
# carries it as historyRecordSinceMs. Every run begun at or after it went out,
# if at all, while this record was being kept, so a run that new with no entry
# never went out through a successful publish (the Blob step failed, or the
# stamp did): a reader treats it as not out. An older run with no entry may
# simply predate the record, and the reader falls back to its own rule.
import datetime as dt
import json
import sys
from pathlib import Path

from .history import KEEP_RUNS

# The history window holds exactly KEEP_RUNS earlier runs; two spare entries
# so a republish of the live run never pushes the oldest one out.
KEEP_RECORDS = KEEP_RUNS + 2

FILE = "published.json"
SINCE = "since"
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _path(state):
    return Path(state) / FILE


def _run_id(run_ms):
    return (_EPOCH + dt.timedelta(milliseconds=int(run_ms))).strftime("%Y%m%d%H")


def _run_ms(run_id):
    t = dt.datetime.strptime(run_id, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    return int((t - _EPOCH).total_seconds() * 1000)


def load(state):
    """{run id: publishedMs}; {} when absent or unreadable (never an error:
    a lost record costs the times, never the build)."""
    p = _path(state)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"::warning::{p} unreadable, ignoring it: {type(e).__name__}: {e}")
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            _run_ms(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out[k] = v
    return out


def since(state):
    """When the record began (see the header), or None: no file, no key, or
    an unreadable one."""
    try:
        v = json.loads(_path(state).read_text()).get(SINCE)
    except Exception:
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def record(state, run_ms, published_ms):
    """Note that run `run_ms` (epoch ms) went out at `published_ms`; a later
    time replaces an earlier one (see the header), and only the KEEP_RECORDS
    newest runs are kept. The first record without a "since" sets it to
    `published_ms`."""
    times = load(state)
    began = since(state) if times else None
    rid = _run_id(run_ms)
    times[rid] = max(times.get(rid, 0), int(published_ms))
    keep = sorted(times)[-KEEP_RECORDS:]
    out = {k: times[k] for k in keep}
    out[SINCE] = began if began is not None else int(published_ms)
    p = _path(state)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))


def history_runs(state, run):
    """[{runMs, publishedMs}, ...] ascending for the recorded runs older than
    `run` (a datetime) — the runs whose heads can sit ahead of it."""
    cur = int((run - _EPOCH).total_seconds() * 1000)
    return [{"runMs": _run_ms(k), "publishedMs": v}
            for k, v in sorted(load(state).items()) if _run_ms(k) < cur]


def main(argv=None):
    """python -m pipeline.published <state dir> <manifest.json>: record the
    manifest's run as published now. Run by the workflows right after the
    Blob publish succeeds, before the state push."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m pipeline.published <state dir> <manifest.json>", file=sys.stderr)
        return 2
    state, manifest = argv
    run_ms = json.loads(Path(manifest).read_text())["runMs"]
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    record(state, run_ms, now_ms)
    print(f"published: run {_run_id(run_ms)} at {now_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
