#!/usr/bin/env bash
# Wrapper for the daily eodhd-ig-margins systemd service: pulls the same
# canonical db used by run_daily_update.sh (sda.db), runs
# scripts/get_ig_margins.py against it, pushes it back, then archives the
# raw downloaded spreadsheet (every country, not just AU/US) to remote
# storage for continuity with its predecessor's monthly archive on the
# old "nebula" machine -- see doc/DESIGN_DECISIONS.md ("IG tiered
# margins").
#
# Shares its pull/push/staleness-check mechanism with run_daily_update.sh
# via ops/lib_rclone_sync.sh, rather than duplicating it -- both wrappers
# can touch the same remote db from independent schedules, so the same
# safe_push() race protection applies here too.
#
# Config comes from ops/daily-update.env (same file run_daily_update.sh
# uses -- RCLONE_REMOTE must point at the same db) plus
# IG_ARCHIVE_RCLONE_REMOTE below, or environment overrides.
#
# Requires jq (for safe_push()'s remote staleness check, via
# lib_rclone_sync.sh) and curl (for the spreadsheet download, via
# scripts/get_ig_margins.py).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${REPO_DIR}/ops/lib_rclone_sync.sh"

RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive:eodhd/prod.db}"
WORK_DIR="${WORK_DIR:-/var/tmp/eodhd-daily-update}"
# Where the raw monthly spreadsheet (all countries) is archived -- a
# different remote path from the db, since it's a different kind of
# artifact (an append-only archive, one new dated file a month, not a
# single mutable file like the db). No staleness check needed here: each
# month's file has a unique name, so there's nothing to race against.
# Default matches where this script's predecessor's archive already
# lives on the old "nebula" machine -- see doc/DESIGN_DECISIONS.md ("IG
# tiered margins").
IG_ARCHIVE_RCLONE_REMOTE="${IG_ARCHIVE_RCLONE_REMOTE:-onedrive:Data/StockDataAnalytics/mars/tiered}"

if [ -f "${REPO_DIR}/ops/daily-update.env" ]; then
    # shellcheck disable=SC1091
    source "${REPO_DIR}/ops/daily-update.env"
fi

# Own subdirectory, same reasoning as run_daily_update.sh's per-mode
# split -- keeps this job's scratch space from colliding with any of the
# tips/price modes if a run ever overlaps.
WORK_DIR="${WORK_DIR}/ig-margins"
DB_PATH="${WORK_DIR}/prod.db"
XLSX_DIR="${WORK_DIR}/xlsx"
PULLED_MODTIME_FILE="${DB_PATH}.pulled_modtime"

mkdir -p "${WORK_DIR}" "${XLSX_DIR}"

recover_leftover_if_any
mkdir -p "${XLSX_DIR}"

pull_db

"${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/get_ig_margins.py" \
    --db-path "${DB_PATH}" --xlsx-dir "${XLSX_DIR}"

# get_ig_margins.py deletes the download when IG's file is a version
# already loaded, and writes nothing. Then there's nothing to push or
# archive: skip the push rather than re-upload an unchanged 1.4 GB db
# every day (and race the other jobs for no reason).
if ! compgen -G "${XLSX_DIR}/*.xlsx" > /dev/null; then
    echo "[daily-update] no new version of IG's file -- nothing to push"
    rm -rf "${WORK_DIR}"
    exit 0
fi

echo "[daily-update] pushing db back to ${RCLONE_REMOTE}"
safe_push

echo "[daily-update] archiving raw spreadsheet(s) to ${IG_ARCHIVE_RCLONE_REMOTE}"
shopt -s nullglob
for xlsx_file in "${XLSX_DIR}"/*.xlsx; do
    if ! rclone copyto "${xlsx_file}" "${IG_ARCHIVE_RCLONE_REMOTE}/$(basename "${xlsx_file}")"; then
        # Soft-fail, not fatal -- the canonical db push above already
        # succeeded, which is the important part. But unlike
        # snapshot_and_prune()'s weekly snapshots, next month's run won't
        # retry *this* month's file -- it downloads its own new one. A
        # failure here is a permanent gap in the archive unless someone
        # notices this warning and re-uploads ${xlsx_file} by hand before
        # WORK_DIR is cleaned up below.
        echo "[daily-update] WARNING: failed to archive $(basename "${xlsx_file}") -- continuing (db push already succeeded; this month's archive copy will be missing unless re-uploaded manually before cleanup)" >&2
    fi
done
shopt -u nullglob

echo "[daily-update] cleaning up local scratch copy"
rm -rf "${WORK_DIR}"

echo "[daily-update] done"
