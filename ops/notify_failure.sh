#!/usr/bin/env bash
# Alerts by SMS and email when one of this project's systemd services
# fails. Run by eodhd-failure-alert@.service, which each service names in
# its OnFailure= line; the failed unit's name arrives as $1. Uses the same
# ~/repos/alerts notifier as check_disk_space.sh (reused as-is).
#
# Added after 2026-11-12, when sda.db was replaced by a OneDrive error page
# and every scheduled run failed for a week before anyone looked (see
# doc/DESIGN_DECISIONS.md, "sda.db replaced by an error page").
#
# Usage: notify_failure.sh <unit-name>
# Config via environment overrides: ALERTS_REPO, FAILURE_LOG_LINES.
set -euo pipefail

UNIT="${1:?usage: notify_failure.sh <unit-name>}"
ALERTS_REPO="${ALERTS_REPO:-/home/ian/repos/alerts}"
LOG_LINES="${FAILURE_LOG_LINES:-40}"

# This invocation's log only, if systemd can say which one it was;
# otherwise the unit's last lines.
invocation="$(systemctl show -p InvocationID --value "${UNIT}" 2>/dev/null || true)"
if [ -n "${invocation}" ]; then
    log="$(journalctl --no-pager -o short "_SYSTEMD_INVOCATION_ID=${invocation}" 2>/dev/null | tail -n "${LOG_LINES}" || true)"
fi
if [ -z "${log:-}" ]; then
    log="$(journalctl --no-pager -o short -u "${UNIT}" -n "${LOG_LINES}" 2>/dev/null || true)"
fi
# Never send the EODHD token (or any other api_token=) in an alert.
log="$(printf '%s\n' "${log}" | sed -E 's/(api_token=)[^&[:space:]]+/\1REDACTED/g')"

result="$(systemctl show -p Result --value "${UNIT}" 2>/dev/null || echo unknown)"
subject="${UNIT} failed on $(hostname)"
sms="${UNIT} failed on $(hostname) (${result}). Log in email."
body="${UNIT} failed on $(hostname) at $(date '+%Y-%m-%d %H:%M %Z') (result: ${result}).

Full log: journalctl -u ${UNIT} -n 200

Last ${LOG_LINES} lines:
${log}"

echo "[failure-alert] ${UNIT} failed (${result}) -- alerting"

cd "${ALERTS_REPO}"
PYTHONPATH=src "${ALERTS_REPO}/.venv/bin/python" - "${subject}" "${sms}" "${body}" <<'PYEOF'
import sys
from notifier import send_email, send_sms

subject, sms, body = sys.argv[1], sys.argv[2], sys.argv[3]

# SMS first, so a failure can be reported in the email -- same pattern as
# check_disk_space.sh and notify_alerts() in the notifier module.
try:
    send_sms(sms)
except Exception as e:
    print(f"SMS FAILED: {e}")
    body += f"\n\nSMS FAILED: {e}"

send_email(subject, body)
PYEOF
