import datetime as dt
import json
from pipeline.dwd import STEPS, FIELDS, file_name
import tick

UTC = dt.timezone.utc
RUN = dt.datetime(2026, 9, 28, 12, tzinfo=UTC)


def _listing(run, field, steps=STEPS):
    return "".join(f'<a href="{file_name(run, s, field)}">x</a>\n' for s in steps)


def _deps(live, run=RUN, hh="12", steps=STEPS):
    def listing(h, field):
        return _listing(run, field, steps) if h == hh else ""

    def fetch_text(url):
        assert url.endswith("manifest.json")
        if live is None:
            raise OSError("404")
        return json.dumps(live)
    return listing, fetch_text


def test_a_newer_complete_run_needs_a_build():
    listing, fetch = _deps({"run": "2026-09-28T00Z", "complete": True})
    assert tick.needs_build(listing, fetch, "https://x") is True


WHOLE = {"columns": {"x": 1}, "thick": {"x": 1}, "blocks": "blocks/x", "tail": {"x": 1}}


def test_the_run_already_live_needs_nothing():
    listing, fetch = _deps({"run": "2026-09-28T12Z", "complete": True, **WHOLE})
    assert tick.needs_build(listing, fetch, "https://x") is False


def test_a_partial_live_copy_of_the_same_run_still_needs_the_full_build():
    listing, fetch = _deps({"run": "2026-09-28T12Z", "complete": False})
    assert tick.needs_build(listing, fetch, "https://x") is True


def test_a_run_dwd_has_not_finished_listing_needs_nothing_yet():
    listing, fetch = _deps({"run": "2026-09-28T00Z", "complete": True, **WHOLE}, steps=STEPS[:10])
    assert tick.needs_build(listing, fetch, "https://x") is False


def test_an_unreadable_live_manifest_counts_as_needing_a_build():
    listing, fetch = _deps(None)
    assert tick.needs_build(listing, fetch, "https://x") is True


# ---- Repairs (2026-10-28: the 18Z run went live without its fog columns) ----

def test_the_live_run_missing_a_family_is_rebuilt():
    for missing in ("columns", "thick", "blocks", "tail"):
        live_m = {"run": "2026-09-28T12Z", "complete": True, **WHOLE, missing: None}
        listing, fetch = _deps(live_m)
        assert tick.needs_build(listing, fetch, "https://x") is True, missing


def test_a_run_already_repaired_twice_is_left_alone():
    listing, fetch = _deps({"run": "2026-09-28T12Z", "complete": True, **WHOLE, "columns": None, "repairs": 2})
    assert tick.needs_build(listing, fetch, "https://x") is False


def test_blob_is_read_first_and_pages_only_when_blob_is_down():
    from pipeline import live
    pages = {"run": "2026-09-28T12Z", "complete": True}                 # Pages never names the families
    def fetch(url):
        if url.startswith(live.BLOB_BASE):
            raise OSError("blob down")
        return json.dumps(pages)
    listing, _ = _deps(None)
    assert tick.needs_build(listing, fetch, "https://pages") is False     # no rebuild loop on a Blob outage
    def fetch_both(url):
        return json.dumps({"run": "2026-09-28T00Z", "complete": True, **WHOLE}) if url.startswith(live.BLOB_BASE) \
            else json.dumps(pages)
    assert tick.needs_build(listing, fetch_both, "https://pages") is True  # Blob is behind: the app's copy wins


def test_http_text_retries_a_dropped_connection_and_a_5xx_but_not_a_4xx():
    import http.client, urllib.error, pytest
    from pipeline import live

    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False
    seq = [http.client.RemoteDisconnected("x"), urllib.error.HTTPError("u", 503, "busy", {}, None), R(b"ok")]
    naps = []
    def urlopen(url, timeout):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    assert live.http_text("u", urlopen=urlopen, sleep=naps.append) == "ok" and naps == [2, 4]
    calls = []
    def gone(url, timeout):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "nope", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        live.http_text("u", urlopen=gone, sleep=naps.append)
    assert len(calls) == 1
