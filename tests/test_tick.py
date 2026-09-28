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


def test_the_run_already_live_needs_nothing():
    listing, fetch = _deps({"run": "2026-09-28T12Z", "complete": True})
    assert tick.needs_build(listing, fetch, "https://x") is False


def test_a_partial_live_copy_of_the_same_run_still_needs_the_full_build():
    listing, fetch = _deps({"run": "2026-09-28T12Z", "complete": False})
    assert tick.needs_build(listing, fetch, "https://x") is True


def test_a_run_dwd_has_not_finished_listing_needs_nothing_yet():
    listing, fetch = _deps({"run": "2026-09-28T00Z", "complete": True}, steps=STEPS[:10])
    assert tick.needs_build(listing, fetch, "https://x") is False


def test_an_unreadable_live_manifest_counts_as_needing_a_build():
    listing, fetch = _deps(None)
    assert tick.needs_build(listing, fetch, "https://x") is True
