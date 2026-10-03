# tools/watchdog-key-setup.sh, run under bash with curl and gh stubbed: it
# must create a send-only key for skycastapp.com, hand it to gh on stdin, and
# never print either key or put one on a command line.
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "watchdog-key-setup.sh"
FULL = "re_FULLaccess_0123456789abcdef"
SEND_ONLY = "re_SENDonly_fedcba9876543210"
DOMAINS = {"object": "list", "data": [
    {"id": "dom_other", "name": "nugshots.com", "status": "verified"},
    {"id": "dom_sky", "name": "skycastapp.com", "status": "verified"},
]}

CURL = textwrap.dedent("""\
    import json, os, sys
    from pathlib import Path
    T = Path(os.environ["T"])
    args = sys.argv[1:]
    headers = sys.stdin.read() if "@-" in args else ""
    method = args[args.index("-X") + 1]
    url = next(a for a in args if a.startswith("https://"))
    data = args[args.index("--data") + 1] if "--data" in args else None
    with open(T / "curl.log", "a") as f:
        f.write(json.dumps({"argv": args, "headers": headers, "method": method, "url": url, "data": data}) + "\\n")
    answers = json.loads((T / "answers.json").read_text())
    status, body = answers.get(method + " " + url.split("api.resend.com", 1)[1], [404, {"message": "no"}])
    sys.stdout.write(json.dumps(body) + "\\n" + str(status))
    """)

GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "$*" >> "$T/gh.log"
    if [ "$1" = "auth" ]; then exit 0; fi
    cat > "$T/gh.stdin"
    exit "${GH_SECRET_RC:-0}"
    """)


def answers(over=None):
    a = {"GET /domains": [200, DOMAINS],
         "POST /api-keys": [201, {"id": "key_new", "token": SEND_ONLY}],
         "DELETE /api-keys/key_new": [200, {}]}
    a.update(over or {})
    return a


def run(tmp_path, answers_=None, key=FULL, gh_rc=0):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(f"#!{sys.executable}\n" + CURL)
    (bin_ / "gh").write_text(GH)
    for f in bin_.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    if key is not None:
        (home / ".config/skycast").mkdir(parents=True)
        (home / ".config/skycast/resend.key").write_text(key + "\n")
    (tmp_path / "answers.json").write_text(json.dumps(answers_ or answers()))
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(home), "T": str(tmp_path),
           "GH_SECRET_RC": str(gh_rc)}
    env.pop("REPO", None)
    env.pop("RESEND_FULL_KEY_FILE", None)
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    log = tmp_path / "curl.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return p, calls


def no_key_leaks(p, calls):
    out = p.stdout + p.stderr
    assert FULL not in out and SEND_ONLY not in out
    for c in calls:
        assert not any(FULL in a or SEND_ONLY in a for a in c["argv"])


def test_it_creates_a_send_only_key_for_skycastapp_and_stores_it(tmp_path):
    p, calls = run(tmp_path)
    assert p.returncode == 0, p.stderr
    no_key_leaks(p, calls)
    assert [(c["method"], c["url"]) for c in calls] == [
        ("GET", "https://api.resend.com/domains"), ("POST", "https://api.resend.com/api-keys")]
    assert all(c["headers"] == f"Authorization: Bearer {FULL}\n" for c in calls)
    body = json.loads(calls[1]["data"])
    assert body["permission"] == "sending_access" and body["domain_id"] == "dom_sky"
    assert body["name"].startswith("skycast-tick-watchdog ")
    gh = (tmp_path / "gh.log").read_text().splitlines()
    assert gh[-1] == "secret set RESEND_ALERTS_KEY --repo eriknugshots/skycast-icon-pipeline"
    assert (tmp_path / "gh.stdin").read_text() == SEND_ONLY
    assert "key_new" in p.stdout and "RESEND_ALERTS_KEY" in p.stdout


def test_a_missing_full_key_stops_before_calling_resend(tmp_path):
    p, calls = run(tmp_path, key=None)
    assert p.returncode != 0 and "no full-access Resend key" in p.stderr
    assert calls == []


def test_an_unknown_domain_stops_without_creating_a_key(tmp_path):
    p, calls = run(tmp_path, answers({"GET /domains": [200, {"data": [{"id": "x", "name": "nugshots.com"}]}]}))
    assert p.returncode != 0 and "skycastapp.com is not a domain" in p.stderr
    assert [c["method"] for c in calls] == ["GET"]
    no_key_leaks(p, calls)


def test_a_refused_create_reports_resends_message_and_stores_nothing(tmp_path):
    p, calls = run(tmp_path, answers({"POST /api-keys": [401, {"message": "API key is invalid"}]}))
    assert p.returncode != 0 and "HTTP 401 API key is invalid" in p.stderr
    assert not (tmp_path / "gh.stdin").exists()
    no_key_leaks(p, calls)


def test_a_failed_gh_secret_set_deletes_the_new_key_again(tmp_path):
    p, calls = run(tmp_path, gh_rc=1)
    assert p.returncode != 0 and "deleted the new key key_new" in p.stderr
    assert (calls[-1]["method"], calls[-1]["url"]) == ("DELETE", "https://api.resend.com/api-keys/key_new")
    no_key_leaks(p, calls)


def test_it_refuses_to_run_traced(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    p = subprocess.run(["bash", "-x", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "do not run with -x" in p.stderr
    assert "resend.key" not in p.stderr.split("do not run with -x")[1]


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
