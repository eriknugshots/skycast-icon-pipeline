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
# The email goes out as SkyCast Admin with the plain text AND the same dark
# HTML card as the app's admin alerts (SkyCast api/_alertmail.js,
# alertEmailHtml): a "SKYCAST ADMIN" line in amber, the subject as the title,
# the lines, the links as amber buttons, one quiet footer. Tables and inline
# styles only, no images or web fonts, every interpolated string escaped.
import argparse
import datetime as dt
import html as htmllib
import json
import os
import sys
import urllib.error
import urllib.request

HEALTH_URL = "https://sunset-prediction.vercel.app/api/alerts-health"
LOGS_URL = "https://vercel.com/basalt-labs/sunset-prediction/logs"
RESEND_URL = "https://api.resend.com/emails"
SENDER = "SkyCast Admin <alerts@skycastapp.com>"
FOOTER = "SkyCast admin alert · sent by the tick watchdog (skycast-icon-pipeline)."
# The app's own tokens (SkyCast index.html), as in api/_alertmail.js.
GROUND, CARD, TEXT, TEXT2, TEXT3, LINE, AMBER = "#050608", "#0c1015", "#f2f4f7", "#aab2c0", "#6b7382", "#1f2630", "#f59e0b"
FONT = "Jost, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
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


def _esc(v):
    return htmllib.escape("" if v is None else str(v), quote=True)


def html_email(title, lines, links, footer=FOOTER):
    """The branded HTML card (the same look as SkyCast's api/_alertmail.js):
    title, paragraphs, links [(label, href)] (the first a filled amber button,
    the rest amber text links; only http(s) addresses), one footer line."""
    font = f"font-family:{FONT};"
    paras = "\n".join(
        f'<p style="margin:0 0 12px;{font}font-size:15px;line-height:1.55;color:{TEXT2};">{_esc(l.strip())}</p>'
        for l in lines if l and l.strip())
    cells = []
    for i, (label, href) in enumerate(l for l in links if l[1] and str(l[1]).lower().startswith(("http://", "https://"))):
        if i == 0:
            cells.append(
                f'<td style="padding:0 12px 8px 0;" valign="middle"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td bgcolor="{AMBER}" style="background-color:{AMBER};border-radius:9px;"><a href="{_esc(href)}" style="display:inline-block;'
                f'padding:11px 18px;{font}font-size:14px;font-weight:600;line-height:1.2;color:{GROUND};text-decoration:none;border-radius:9px;">'
                f'{_esc(label)}</a></td></tr></table></td>')
        else:
            cells.append(
                f'<td style="padding:0 12px 8px 0;" valign="middle"><a href="{_esc(href)}" style="display:inline-block;padding:11px 4px;'
                f'{font}font-size:14px;font-weight:600;line-height:1.2;color:{AMBER};text-decoration:underline;">{_esc(label)}</a></td>')
    buttons = (f'<tr><td style="padding:10px 28px 18px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
               f'{"".join(cells)}</tr></table></td></tr>\n') if cells else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_esc(title)}</title>
<style>:root {{ color-scheme: dark; supported-color-schemes: dark; }}</style>
</head>
<body bgcolor="{GROUND}" style="margin:0;padding:0;background-color:{GROUND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{GROUND}" style="background-color:{GROUND};">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{CARD}" style="max-width:560px;background-color:{CARD};border:1px solid {LINE};border-radius:14px;">
<tr><td style="padding:26px 28px 0;{font}font-size:11px;font-weight:600;letter-spacing:0.22em;text-transform:uppercase;color:{AMBER};">SKYCAST ADMIN</td></tr>
<tr><td style="padding:12px 28px 6px;"><h1 style="margin:0;{font}font-size:21px;font-weight:600;line-height:1.3;color:{TEXT};">{_esc(title)}</h1></td></tr>
<tr><td style="padding:10px 28px 4px;">
{paras}
</td></tr>
{buttons}<tr><td style="padding:16px 28px 24px;border-top:1px solid {LINE};{font}font-size:12px;line-height:1.5;color:{TEXT3};">{_esc(footer)}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def compose_html(subject, text, health_url=HEALTH_URL):
    """The card for a composed email: its subject, its lines up to the link
    lines (which become the buttons), the logs and the health check."""
    lines = [l for l in text.splitlines()
             if l.strip() and not l.startswith(("Health: ", "Logs: ", "Sent by the tick watchdog"))]
    return html_email(subject, lines, [("Open the logs", LOGS_URL), ("Health check", health_url)])


def send(http, key, subject, text, html=None):
    """(sent, detail) for one email through Resend's HTTP API. html, when
    given, is the branded card; text stays as every client's fallback."""
    doc = {"from": SENDER, "to": RECIPIENTS, "subject": subject, "text": text}
    if html:
        doc["html"] = html
    payload = json.dumps(doc).encode()
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


def wake(prev, key, http, now, log, name="alerts", health_url=HEALTH_URL, fields_wanted=FIELDS, absent_ok=False):
    """One wake of the tick: check, log, maybe email. Returns the next state
    record {"state", "since"}. absent_ok: a 404 means the route is not
    deployed yet (a check added ahead of its server), so the wake logs it and
    changes nothing; every other failure counts as before."""
    prev = prev if isinstance(prev, dict) and prev.get("state") in (START, OK, DOWN) else {"state": START}
    state = prev["state"]
    healthy, cause, fields = check(http, health_url, fields_wanted)
    if absent_ok and cause == "HTTP 404":
        log(f"watchdog: {name}: {health_url} is not deployed yet (404), nothing to watch")
        return prev
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
    sent, detail = send(http, key, subject, text, compose_html(subject, text, health_url))
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
        p.add_argument("--absent-ok", action="store_true")
        a = p.parse_args(argv)
        now = now or dt.datetime.now(dt.timezone.utc)
        key = os.environ.get(KEY_ENV, "").strip()
        fields = tuple(f.strip() for f in a.fields.split(",") if f.strip()) or FIELDS
        save(a.state_file, wake(load(a.state_file), key, http, now, log,
                                a.name, a.health_url, fields, a.absent_ok))
    except (Exception, SystemExit) as e:
        log(f"watchdog: skipped this wake ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
