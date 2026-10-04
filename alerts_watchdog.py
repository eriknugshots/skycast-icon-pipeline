#!/usr/bin/env python3
# alerts_watchdog.py — is the SkyCast alerts server's cron alive? tick.yml runs
# this once per wake of its loop, in the background and capped at 60 s, so it
# can neither fail nor delay the tick. It GETs the alerts server's health route
# and emails Erik through Resend when that changes. GitHub does not share
# Vercel's fate: if the Vercel project dies, this still runs. Stdlib only, like
# tick.py (the tick installs nothing).
#
# State lives in --state-file, which tick.yml keeps in the tick job's
# RUNNER_TEMP and removes when the loop starts: one tick (5 h) is one chain.
#   start --ok-->     ok     quiet
#   start --not ok--> down   email "down" (the chain starts down)
#   ok    --not ok--> down   email "down"
#   down  --not ok--> down   quiet: at most one "down" email per chain
#   down  --ok-->     ok     email "recovered"
# The state moves on once the email is sent, or skipped for a missing key; a
# send that fails leaves it where it was, so the next wake tries again.
# main() never raises and always exits 0.
# --health-url, --name and --fields point the same watchdog at another health
# route (the tick runs a second one for the tile key job); the defaults are the
# alerts check exactly, and each check keeps its own --state-file.
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

HEALTH_URL = "https://sunset-prediction.vercel.app/api/alerts-health"
LOGS_URL = "https://vercel.com/sky-cast-d0844559/sunset-prediction/logs"
RESEND_URL = "https://api.resend.com/emails"
SENDER = "SkyCast alerts <alerts@skycastapp.com>"
RECIPIENTS = ["erik@nugshots.com", "info@skycastapp.com"]
KEY_ENV = "RESEND_ALERTS_KEY"
HEALTH_TIMEOUT = 15
SEND_TIMEOUT = 15
FIELDS = ("ok", "lastRunAgeMin", "lastOkAgeMin", "lastErrorKind")
USER_AGENT = "skycast-tick-watchdog (github.com/eriknugshots/skycast-icon-pipeline)"

START, OK, DOWN = "start", "ok", "down"


def http_request(method, url, headers, body, timeout):
    """(status, body bytes) for any HTTP answer; raises on a network error."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        try:
            data = e.read(65536)
        except Exception:
            data = b""
        return e.code, data


def _reason(e):
    reason = getattr(e, "reason", None) or e
    return str(reason) or type(reason).__name__


def check(http, health_url=HEALTH_URL, fields_wanted=FIELDS):
    """(healthy, cause, fields): cause says why not; fields are whatever of the
    health route's fields came back (none on a network error)."""
    try:
        status, body = http("GET", health_url, {"Accept": "application/json"}, None, HEALTH_TIMEOUT)
    except Exception as e:
        return False, f"network error ({_reason(e)})", {}
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        doc = None
    fields = {k: doc[k] for k in fields_wanted if k in doc} if isinstance(doc, dict) else {}
    if status != 200:
        return False, f"HTTP {status}", fields
    if not isinstance(doc, dict):
        return False, "HTTP 200 but not a JSON object", fields
    if doc.get("ok") is not True:
        return False, f"ok:{json.dumps(doc['ok'])}" if "ok" in doc else "ok missing", fields
    return True, None, fields


def step(state, healthy):
    """(next state, email to send or None)."""
    if healthy:
        return OK, "recovered" if state == DOWN else None
    return DOWN, None if state == DOWN else "down"


def _value(fields, k):
    if k not in fields:
        return "n/a"
    v = fields[k]
    return v if isinstance(v, str) else json.dumps(v)


def _fields_line(fields, fields_wanted=FIELDS):
    return " ".join(f"{k}={_value(fields, k)}" for k in fields_wanted)


def _hhmm(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return None


def compose(kind, cause, fields, now, down_since=None, name="alerts", health_url=HEALTH_URL, fields_wanted=FIELDS):
    """(subject, plain text) for a "down" or "recovered" email."""
    if kind == "down":
        subject = f"SkyCast {name} server down: {cause}"
        head = f"The SkyCast {name} server's health check is not ok: {cause}."
    else:
        subject = f"SkyCast {name} server recovered"
        since = _hhmm(down_since)
        head = f"The SkyCast {name} server's health check is ok again" + (
            f" (this tick first saw it down at {since})." if since else ".")
    lines = [head, ""]
    lines += [f"{k}: {_value(fields, k)}" for k in fields_wanted]
    lines += ["",
              f"Checked: {now.strftime('%Y-%m-%d %H:%M UTC')}",
              f"Health: {health_url}",
              f"Logs: {LOGS_URL}",
              "",
              "Sent by the tick watchdog (eriknugshots/skycast-icon-pipeline, .github/workflows/tick.yml)."]
    return subject, "\n".join(lines) + "\n"


def send(http, key, subject, text):
    """(sent, detail) for one email through Resend's HTTP API."""
    payload = json.dumps({"from": SENDER, "to": RECIPIENTS, "subject": subject, "text": text}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        status, body = http("POST", RESEND_URL, headers, payload, SEND_TIMEOUT)
    except Exception as e:
        return False, f"network error ({_reason(e)})"
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    if status in (200, 201):
        return True, f"id {doc.get('id', '?')}"
    return False, f"HTTP {status} {str(doc.get('message', ''))[:200]}".rstrip()


def wake(prev, key, http, now, log, name="alerts", health_url=HEALTH_URL, fields_wanted=FIELDS):
    """One wake of the tick: check, log, maybe email. Returns the next state
    record {"state", "since"}."""
    prev = prev if isinstance(prev, dict) and prev.get("state") in (START, OK, DOWN) else {"state": START}
    state = prev["state"]
    healthy, cause, fields = check(http, health_url, fields_wanted)
    log(f"watchdog: {'ok' if healthy else 'NOT OK, ' + cause} ({_fields_line(fields, fields_wanted)})")
    new, kind = step(state, healthy)
    nxt = {"state": new, "since": prev.get("since") if new == state else now.isoformat(timespec="seconds")}
    if kind is None:
        return nxt
    if not key:
        log(f"watchdog: {KEY_ENV} is not set, not emailing \"{kind}\"")
        return nxt
    subject, text = compose(kind, cause, fields, now, prev.get("since") if kind == "recovered" else None,
                            name, health_url, fields_wanted)
    sent, detail = send(http, key, subject, text)
    if sent:
        log(f"watchdog: emailed \"{kind}\" to {', '.join(RECIPIENTS)} ({detail})")
        return nxt
    log(f"watchdog: emailing \"{kind}\" failed, {detail}; trying again next wake")
    return prev


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, record):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def main(argv=None, http=http_request, now=None, log=None):
    log = log or (lambda line: print(line, flush=True))
    try:
        p = argparse.ArgumentParser()
        p.add_argument("--state-file", required=True)
        p.add_argument("--health-url", default=HEALTH_URL)
        p.add_argument("--name", default="alerts")
        p.add_argument("--fields", default=",".join(FIELDS))
        a = p.parse_args(argv)
        now = now or dt.datetime.now(dt.timezone.utc)
        key = os.environ.get(KEY_ENV, "").strip()
        fields = tuple(f.strip() for f in a.fields.split(",") if f.strip()) or FIELDS
        save(a.state_file, wake(load(a.state_file), key, http, now, log,
                                a.name, a.health_url, fields))
    except (Exception, SystemExit) as e:
        log(f"watchdog: skipped this wake ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
