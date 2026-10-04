import datetime as dt
import json
import socket
import urllib.error

import pytest

import alerts_watchdog as wd

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 2, 21, 40, tzinfo=UTC)
HEALTHY = {"ok": True, "lastRunAgeMin": 3, "lastOkAgeMin": 3, "lastErrorKind": None}
FAILING = {"ok": False, "lastRunAgeMin": 4, "lastOkAgeMin": 95, "lastErrorKind": "apns"}


class Http:
    """Stub of http_request: one health answer per wake, Resend answers 200
    unless told otherwise; records every call."""

    def __init__(self, health, resend=(200, {"id": "em_1"})):
        self.health, self.resend, self.calls = health, resend, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        answer = self.health if url == wd.HEALTH_URL else self.resend
        if isinstance(answer, BaseException):
            raise answer
        status, doc = answer
        return status, doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    @property
    def emails(self):
        return [json.loads(c[3]) for c in self.calls if c[1] == wd.RESEND_URL]


def ok():
    return Http((200, HEALTHY))


def down():
    return Http((200, FAILING))


def run_chain(https, key="re_test", start=None):
    """Wakes 10 minutes apart through one tick; returns (state, emails, log)."""
    state, emails, log = start, [], []
    for i, http in enumerate(https):
        state = wd.wake(state, key, http, T0 + dt.timedelta(minutes=10 * i), log.append)
        emails += http.emails
    return state, emails, log


# --- the state machine ------------------------------------------------------

@pytest.mark.parametrize("state, healthy, expected", [
    (wd.START, True, (wd.OK, None)),
    (wd.START, False, (wd.DOWN, "down")),
    (wd.OK, True, (wd.OK, None)),
    (wd.OK, False, (wd.DOWN, "down")),
    (wd.DOWN, False, (wd.DOWN, None)),
    (wd.DOWN, True, (wd.OK, "recovered")),
])
def test_step_covers_every_transition(state, healthy, expected):
    assert wd.step(state, healthy) == expected


def test_a_healthy_chain_sends_nothing():
    state, emails, _ = run_chain([ok(), ok(), ok()])
    assert state["state"] == wd.OK and emails == []


def test_ok_to_down_emails_once_and_down_stays_quiet_within_the_chain():
    state, emails, _ = run_chain([ok(), down(), down(), down(), down()])
    assert state["state"] == wd.DOWN
    assert len(emails) == 1
    assert "down" in emails[0]["subject"]


def test_down_to_ok_emails_recovered():
    state, emails, _ = run_chain([ok(), down(), down(), ok(), ok()])
    assert state["state"] == wd.OK
    assert [("down" in e["subject"], "recovered" in e["subject"]) for e in emails] == [(True, False), (False, True)]
    assert "first saw it down at 2026-10-02 21:50 UTC" in emails[1]["text"]


def test_down_again_after_recovering_emails_again():
    _, emails, _ = run_chain([down(), ok(), down()])
    assert ["down" in e["subject"] for e in emails] == [True, False, True]


def test_a_chain_that_starts_down_emails_at_its_start_once():
    state, emails, _ = run_chain([down(), down(), down()])
    assert state["state"] == wd.DOWN and len(emails) == 1


def test_a_failed_send_is_retried_on_the_next_wake_and_only_until_it_goes_through():
    refused = Http((200, FAILING), resend=(500, {"message": "internal"}))
    unreachable = Http((200, FAILING), resend=socket.timeout("timed out"))
    state, _, log = run_chain([ok(), refused, unreachable, down(), down()])
    assert state["state"] == wd.DOWN
    assert [len(h.emails) for h in (refused, unreachable)] == [1, 1]
    assert any("HTTP 500 internal; trying again next wake" in line for line in log)
    sent = [line for line in log if line.startswith('watchdog: emailed "down"')]
    assert len(sent) == 1


def test_a_corrupt_or_missing_state_counts_as_the_start_of_a_chain():
    for prev in (None, {}, {"state": "sideways"}, ["down"]):
        assert wd.wake(prev, "re_test", down(), T0, lambda _: None)["state"] == wd.DOWN


# --- the missing secret -----------------------------------------------------

@pytest.mark.parametrize("key", ["", None])
def test_a_missing_key_logs_one_line_skips_the_email_and_still_tracks_state(key):
    https = [ok(), down(), down()]
    state, emails, log = run_chain(https, key=key)
    assert emails == [] and all(c[1] != wd.RESEND_URL for h in https for c in h.calls)
    assert state["state"] == wd.DOWN
    skips = [line for line in log if "RESEND_ALERTS_KEY is not set" in line]
    assert skips == ['watchdog: RESEND_ALERTS_KEY is not set, not emailing "down"']


def test_main_without_the_key_exits_0_and_writes_state(tmp_path, monkeypatch):
    monkeypatch.delenv("RESEND_ALERTS_KEY", raising=False)
    path, log, http = tmp_path / "s.json", [], down()
    assert wd.main(["--state-file", str(path)], http=http, now=T0, log=log.append) == 0
    assert json.loads(path.read_text())["state"] == wd.DOWN
    assert http.emails == []


# --- each not-ok cause ------------------------------------------------------

@pytest.mark.parametrize("health, cause", [
    (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "network error"),
    (socket.timeout("timed out"), "network error (timed out)"),
    (ConnectionResetError(104, "Connection reset by peer"), "network error"),
    ((404, b"<html>Not Found</html>"), "HTTP 404"),
    ((503, FAILING), "HTTP 503"),
    ((200, FAILING), "ok:false"),
    ((200, {**HEALTHY, "ok": None}), "ok:null"),
    ((200, {"lastRunAgeMin": 3}), "ok missing"),
    ((200, b"<html>maintenance</html>"), "HTTP 200 but not a JSON object"),
])
def test_each_not_ok_cause_is_named_in_the_log_and_the_email(health, cause):
    http = Http(health)
    log = []
    wd.wake({"state": wd.OK}, "re_test", http, T0, log.append)
    assert cause in log[0] and log[0].startswith("watchdog: NOT OK, ")
    [email] = http.emails
    assert email["subject"].startswith("SkyCast alerts server down: ") and cause in email["subject"]
    assert cause in email["text"]


def test_the_health_check_uses_a_15_s_timeout():
    http = ok()
    wd.check(http)
    assert http.calls == [("GET", wd.HEALTH_URL, {"Accept": "application/json"}, None, 15)]


# --- the emails -------------------------------------------------------------

def test_the_down_email_is_plain_short_text_with_fields_time_and_logs():
    http = Http((503, FAILING))
    wd.wake({"state": wd.OK}, "re_secret", http, T0, lambda _: None)
    [(method, url, headers, body, timeout)] = [c for c in http.calls if c[1] == wd.RESEND_URL]
    email = json.loads(body)
    assert (method, url, timeout) == ("POST", "https://api.resend.com/emails", 15)
    assert headers["Authorization"] == "Bearer re_secret"
    assert email["from"] == "SkyCast alerts <alerts@skycastapp.com>"
    assert email["to"] == ["erik@nugshots.com", "info@skycastapp.com"]
    assert set(email) == {"from", "to", "subject", "text"}
    assert email["subject"] == "SkyCast alerts server down: HTTP 503"
    text = email["text"]
    for line in ("ok: false", "lastRunAgeMin: 4", "lastOkAgeMin: 95", "lastErrorKind: apns",
                 "Checked: 2026-10-02 21:40 UTC",
                 "Logs: https://vercel.com/sky-cast-d0844559/sunset-prediction/logs"):
        assert line in text.splitlines()
    assert len(text.splitlines()) < 15


def test_a_network_error_email_marks_the_fields_unavailable():
    http = Http(socket.timeout("timed out"))
    wd.wake({"state": wd.OK}, "re_test", http, T0, lambda _: None)
    assert "lastRunAgeMin: n/a" in http.emails[0]["text"].splitlines()


def test_the_recovered_email_says_recovered():
    http = ok()
    wd.wake({"state": wd.DOWN, "since": T0.isoformat()}, "re_test", http, T0 + dt.timedelta(hours=1), lambda _: None)
    [email] = http.emails
    assert email["subject"] == "SkyCast alerts server recovered"
    assert "ok: true" in email["text"] and "Checked: 2026-10-02 22:40 UTC" in email["text"]


# --- main never fails -------------------------------------------------------

def test_main_survives_a_broken_state_dir_and_bad_arguments(tmp_path):
    log = []
    assert wd.main(["--state-file", str(tmp_path / "no" / "dir" / "s.json")], http=ok(), now=T0, log=log.append) == 0
    assert wd.main([], http=ok(), now=T0, log=log.append) == 0
    assert sum("skipped this wake" in line for line in log) == 2


def test_main_carries_state_between_wakes_through_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_ALERTS_KEY", "re_test")
    path = str(tmp_path / "s.json")
    sent = []
    for i, http in enumerate([ok(), down(), down(), ok()]):
        wd.main(["--state-file", path], http=http, now=T0 + dt.timedelta(minutes=10 * i), log=lambda _: None)
        sent += [e["subject"] for e in http.emails]
    assert sent == ["SkyCast alerts server down: ok:false", "SkyCast alerts server recovered"]


# --- a second check: the tile key job ---------------------------------------

TILES_URL = "https://sunset-prediction.vercel.app/api/tiles-health"
TILES_FIELDS = "ok,newestKeyAgeH,lastRunAgeH,lastErrorKind"
TILES_FAILING = {"ok": False, "newestKeyAgeH": 61, "lastRunAgeH": 26, "lastErrorKind": "google"}


class TilesHttp(Http):
    """Same stub, answering for the tiles health URL instead of the alerts one."""

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        answer = self.health if url == TILES_URL else self.resend
        status, doc = answer
        return status, json.dumps(doc).encode()


def tiles_args(path):
    return ["--state-file", str(path), "--health-url", TILES_URL, "--name", "tile keys",
            "--fields", TILES_FIELDS]


def test_a_down_tiles_check_sends_one_email_named_for_tile_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_ALERTS_KEY", "re_test")
    http, log = TilesHttp((200, TILES_FAILING)), []
    assert wd.main(tiles_args(tmp_path / "t.json"), http=http, now=T0, log=log.append) == 0
    assert [c[1] for c in http.calls][0] == TILES_URL
    [email] = http.emails
    assert "tile keys" in email["subject"] and "down" in email["subject"]
    assert email["subject"] == "SkyCast tile keys server down: ok:false"
    lines = email["text"].splitlines()
    assert "newestKeyAgeH: 61" in lines and "lastRunAgeH: 26" in lines
    assert "lastRunAgeMin: n/a" not in lines and not any(l.startswith("lastRunAgeMin") for l in lines)
    assert f"Health: {TILES_URL}" in lines
    assert "tile keys" in email["text"].splitlines()[0]
    assert json.loads((tmp_path / "t.json").read_text())["state"] == wd.DOWN


def test_the_tiles_check_recovers_with_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_ALERTS_KEY", "re_test")
    path = tmp_path / "t.json"
    sent = []
    for i, doc in enumerate([TILES_FAILING, TILES_FAILING, {**TILES_FAILING, "ok": True}]):
        http = TilesHttp((200, doc))
        wd.main(tiles_args(path), http=http, now=T0 + dt.timedelta(minutes=10 * i), log=lambda _: None)
        sent += [e["subject"] for e in http.emails]
    assert sent == ["SkyCast tile keys server down: ok:false", "SkyCast tile keys server recovered"]


def test_with_no_new_arguments_the_url_and_subject_are_exactly_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_ALERTS_KEY", "re_test")
    http = down()
    wd.main(["--state-file", str(tmp_path / "s.json")], http=http, now=T0, log=lambda _: None)
    assert http.calls[0][1] == "https://sunset-prediction.vercel.app/api/alerts-health"
    [email] = http.emails
    assert email["subject"] == "SkyCast alerts server down: ok:false"
    assert "lastRunAgeMin: 4" in email["text"].splitlines()
    assert "Health: https://sunset-prediction.vercel.app/api/alerts-health" in email["text"].splitlines()
