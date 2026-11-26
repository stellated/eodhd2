#!/usr/bin/env bash
# Wrapper for scripts/get_optionable.py (daily US/AU optionable-stock
# capture; see doc/DESIGN_DECISIONS.md, "Optionable stocks, captured
# daily"). Like alphanume.db, optionable.db lives on this machine's disk
# and is only copied to OneDrive after each run (a consistent snapshot via
# SQLite's backup API), never pulled back; nothing else writes it.
# The raw downloads are copied to OneDrive too, and kept locally for 14
# days. A failed upload is logged, not fatal: the next run uploads again.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${REPO_DIR}/ops/daily-update.env" ]; then
    # shellcheck disable=SC1091
    source "${REPO_DIR}/ops/daily-update.env"
fi
OPTIONABLE_DB="${OPTIONABLE_DB:-${HOME}/data/optionable.db}"
OPTIONABLE_RAW="${OPTIONABLE_RAW:-${HOME}/data/optionable_raw}"
OPTIONABLE_RCLONE_REMOTE="${OPTIONABLE_RCLONE_REMOTE:-onedrive:Data/StockDataAnalytics/mars/optionable.db}"
OPTIONABLE_RAW_REMOTE="${OPTIONABLE_RAW_REMOTE:-onedrive:Data/StockDataAnalytics/mars/optionable_raw}"
PY="${REPO_DIR}/.venv/bin/python"

mkdir -p "$(dirname "${OPTIONABLE_DB}")" "${OPTIONABLE_RAW}"
cd "${REPO_DIR}"  # get_optionable.py reads EODHD_API_TOKEN from .env here

status=0
"${PY}" scripts/get_optionable.py --db-path "${OPTIONABLE_DB}" \
    --archive-dir "${OPTIONABLE_RAW}" "$@" || status=$?

SNAPSHOT="$(mktemp "${OPTIONABLE_DB}.upload.XXXXXX")"
trap 'rm -f "${SNAPSHOT}"' EXIT
if "${PY}" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
        "${OPTIONABLE_DB}" "${SNAPSHOT}" \
        && rclone copyto "${SNAPSHOT}" "${OPTIONABLE_RCLONE_REMOTE}"; then
    echo "[optionable] copied db to ${OPTIONABLE_RCLONE_REMOTE}"
else
    echo "[optionable] WARNING: db upload to ${OPTIONABLE_RCLONE_REMOTE} failed; next run retries" >&2
fi
if rclone copy "${OPTIONABLE_RAW}" "${OPTIONABLE_RAW_REMOTE}" \
        && rclone move "${OPTIONABLE_RAW}" "${OPTIONABLE_RAW_REMOTE}" --min-age 14d; then
    find "${OPTIONABLE_RAW}" -mindepth 1 -type d -empty -delete
    echo "[optionable] raw files copied to ${OPTIONABLE_RAW_REMOTE}"
else
    echo "[optionable] WARNING: raw upload to ${OPTIONABLE_RAW_REMOTE} failed; next run retries" >&2
fi
exit "${status}"
