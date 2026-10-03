#!/usr/bin/env bash
# tools/watchdog-key-setup.sh — run once, on Erik's machine, to give the tick
# watchdog (alerts_watchdog.py) its email key. From the full-access Resend key
# in ~/.config/skycast/resend.key it creates a send-only key restricted to
# skycastapp.com, then stores it as this repo's Actions secret
# RESEND_ALERTS_KEY with `gh secret set`. Neither key is ever printed, put on a
# command line (ps) or written to a file: curl reads the full key as a header
# on stdin, gh reads the new key on stdin.
#
# Needs: bash, curl, python3, gh (logged in with access to the repo).
# Usage: tools/watchdog-key-setup.sh
# Rerunning creates another key; delete the older "skycast-tick-watchdog"
# keys at https://resend.com/api-keys afterwards.
set -euo pipefail
case $- in *x*) echo "watchdog-key-setup: do not run with -x, it would print the keys" >&2; exit 1 ;; esac

REPO="${REPO:-eriknugshots/skycast-icon-pipeline}"
DOMAIN="skycastapp.com"
SECRET="RESEND_ALERTS_KEY"
FULL_KEY_FILE="${RESEND_FULL_KEY_FILE:-$HOME/.config/skycast/resend.key}"
API="https://api.resend.com"

die() { echo "watchdog-key-setup: $*" >&2; exit 1; }

for tool in curl python3 gh; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found"
done
[ -r "$FULL_KEY_FILE" ] || die "no full-access Resend key at $FULL_KEY_FILE"
gh auth status >/dev/null 2>&1 || die "gh is not logged in (run: gh auth login)"

full_key=$(tr -d ' \t\r\n' < "$FULL_KEY_FILE")
[ -n "$full_key" ] || die "$FULL_KEY_FILE is empty"

# resend METHOD PATH [JSON] -> the response body, then the HTTP status on a
# line of its own.
resend() {
  if [ $# -ge 3 ]; then
    printf 'Authorization: Bearer %s\n' "$full_key" |
      curl -sS -X "$1" "$API$2" -H @- -H 'Content-Type: application/json' \
        -H 'User-Agent: skycast-watchdog-key-setup' --data "$3" -w '\n%{http_code}'
  else
    printf 'Authorization: Bearer %s\n' "$full_key" |
      curl -sS -X "$1" "$API$2" -H @- -H 'User-Agent: skycast-watchdog-key-setup' -w '\n%{http_code}'
  fi
}

# json EXPR: evaluate EXPR on the JSON document on stdin (as d); print the
# result, or nothing if it fails. Used only on answers that hold no key.
json() {
  python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
    v = eval(sys.argv[1])
except Exception:
    sys.exit(1)
print("" if v is None else v)' "$1" 2>/dev/null || true
}

# --- the domain -------------------------------------------------------------
resp=$(resend GET /domains) || die "could not reach Resend"
status=${resp##*$'\n'}
body=${resp%$'\n'*}
[ "$status" = "200" ] || die "listing domains failed: HTTP $status $(printf '%s' "$body" | json 'd.get("message", "")')"
pick="[x for x in d.get('data', []) if x.get('name') == '$DOMAIN']"
domain_id=$(printf '%s' "$body" | json "$pick[0]['id']")
[ -n "$domain_id" ] || die "$DOMAIN is not a domain of this Resend account"
domain_status=$(printf '%s' "$body" | json "$pick[0].get('status', '?')")
echo "domain $DOMAIN: $domain_id ($domain_status)"
[ "$domain_status" = "verified" ] || echo "warning: $DOMAIN is not verified yet; sending fails until it is" >&2

# --- the send-only key ------------------------------------------------------
name="skycast-tick-watchdog $(date -u +%Y-%m-%d)"
payload=$(python3 -c 'import json, sys; print(json.dumps({"name": sys.argv[1], "permission": "sending_access", "domain_id": sys.argv[2]}))' "$name" "$domain_id")
resp=$(resend POST /api-keys "$payload") || die "could not reach Resend to create the key"
status=${resp##*$'\n'}
body=${resp%$'\n'*}
case "$status" in
  200|201) ;;
  *) die "creating the key failed: HTTP $status $(printf '%s' "$body" | json 'd.get("message", "")')" ;;
esac
key_id=$(printf '%s' "$body" | json 'd["id"]')
token_ok=$(printf '%s' "$body" | json 'str(d["token"]).startswith("re_")')
[ "$token_ok" = "True" ] && [ -n "$key_id" ] || die "Resend's answer held no key; nothing stored (check https://resend.com/api-keys)"
echo "created send-only key \"$name\" ($key_id), restricted to $DOMAIN"

# --- the secret -------------------------------------------------------------
if printf '%s' "$body" | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["token"])' |
    gh secret set "$SECRET" --repo "$REPO" >/dev/null; then
  echo "stored it as the Actions secret $SECRET on $REPO"
  echo "the next tick (or Actions > tick > Run workflow) uses it"
else
  resp=$(resend DELETE "/api-keys/$key_id") || resp=$'\n000'
  if [ "${resp##*$'\n'}" = "200" ]; then
    die "gh secret set failed; deleted the new key $key_id again, nothing changed"
  fi
  die "gh secret set failed and deleting the new key $key_id failed too; delete it at https://resend.com/api-keys"
fi
