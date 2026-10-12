#!/usr/bin/env bash
# Alerts if spare disk space on this VM drops below a threshold, via the
# same email/SMS channel already used for stock-price alerts in
# ~/repos/alerts (src/notifier.py -- reused as-is, not modified). This is
# a host-health check, deliberately separate from run_daily_update.sh:
# disk exhaustion is a different concern from the data pipeline, and
# needs checking on its own, more frequent schedule (its own systemd
# timer), not only after a successful Sunday run -- see
# doc/DESIGN_DECISIONS.md ("disk-space alert").
#
# Usage: check_disk_space.sh [path-to-check]
# Config via environment overrides: DISK_ALERT_THRESHOLD_BYTES,
# ALERTS_REPO.
set -euo pipefail

CHECK_PATH="${1:-/}"
THRESHOLD_BYTES="${DISK_ALERT_THRESHOLD_BYTES:-1073741824}"  # 1 GiB
ALERTS_REPO="${ALERTS_REPO:-/home/ian/repos/alerts}"

avail_bytes="$(df --output=avail -B1 "${CHECK_PATH}" | tail -1 | tr -d ' ')"

if [ "${avail_bytes}" -ge "${THRESHOLD_BYTES}" ]; then
    echo "[disk-space-alert] ${avail_bytes} bytes free on ${CHECK_PATH} -- above threshold (${THRESHOLD_BYTES}), no alert"
    exit 0
fi

echo "[disk-space-alert] ${avail_bytes} bytes free on ${CHECK_PATH} -- below threshold (${THRESHOLD_BYTES}), alerting"

avail_mb=$(( avail_bytes / 1024 / 1024 ))
threshold_mb=$(( THRESHOLD_BYTES / 1024 / 1024 ))
subject="Low disk space on $(hostname)"
body="Only ${avail_mb}MB free on ${CHECK_PATH} (threshold: ${threshold_mb}MB)."

cd "${ALERTS_REPO}"
PYTHONPATH=src "${ALERTS_REPO}/.venv/bin/python" - "${subject}" "${body}" <<'PYEOF'
import sys
from notifier import send_email, send_sms

subject, body = sys.argv[1], sys.argv[2]

# SMS first, so a failure can be reported in the email -- same pattern as
# notify_alerts() in this same module.
try:
    send_sms(body)
except Exception as e:
    print(f"SMS FAILED: {e}")
    body += f"\n\nSMS FAILED: {e}"

send_email(subject, body)
PYEOF
