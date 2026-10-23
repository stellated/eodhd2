#!/usr/bin/env bash
# Wrapper for scripts/get_alphanume.py (alphanume trial captures -- see
# DAProjects/Alphanume Trial Plan.md). Usage:
#   ops/run_alphanume.sh live premarket_drop      (what the timers run)
#   ops/run_alphanume.sh pull --all               (history, run by hand)
#
# Unlike sda.db, alphanume.db lives on this machine's own disk and is only
# *copied* to OneDrive after each run, never pulled back: a history pull
# can take hours, and the live-capture timers must be able to write while
# it runs. SQLite (WAL mode, set by the script) handles the concurrent
# writers; the copy is a consistent snapshot taken with SQLite's backup
# API, so a run that's still writing can't produce a torn upload. A failed
# upload is logged, not fatal -- the next run uploads again.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${REPO_DIR}/ops/daily-update.env" ]; then
    # shellcheck disable=SC1091
    source "${REPO_DIR}/ops/daily-update.env"
fi
ALPHANUME_DB="${ALPHANUME_DB:-${HOME}/data/alphanume.db}"
ALPHANUME_RCLONE_REMOTE="${ALPHANUME_RCLONE_REMOTE:-onedrive:Data/StockDataAnalytics/mars/alphanume.db}"
PY="${REPO_DIR}/.venv/bin/python"

mkdir -p "$(dirname "${ALPHANUME_DB}")"
cd "${REPO_DIR}"  # get_alphanume.py reads ALPHANUME_API_KEY from .env here

status=0
"${PY}" scripts/get_alphanume.py --db-path "${ALPHANUME_DB}" "$@" || status=$?

SNAPSHOT="$(mktemp "${ALPHANUME_DB}.upload.XXXXXX")"
trap 'rm -f "${SNAPSHOT}"' EXIT
if "${PY}" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
        "${ALPHANUME_DB}" "${SNAPSHOT}" \
        && rclone copyto "${SNAPSHOT}" "${ALPHANUME_RCLONE_REMOTE}"; then
    echo "[alphanume] copied to ${ALPHANUME_RCLONE_REMOTE}"
else
    echo "[alphanume] WARNING: upload to ${ALPHANUME_RCLONE_REMOTE} failed; next run retries" >&2
fi
exit "${status}"
