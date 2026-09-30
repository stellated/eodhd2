#!/usr/bin/env bash
# Wrapper for the systemd service: keeps this VM stateless with respect to
# the database. Pulls the canonical db down from remote storage (rclone),
# runs the update, pushes it back, then deletes the local scratch copy.
#
# Usage: run_daily_update.sh <tips-only|daily-price|intraday-price> [extra daily_update.py args, e.g. --all-emails]
# The mode is baked into each of the three .service units' ExecStart= --
# see doc/DESIGN_DECISIONS.md ("weekday/Saturday/Sunday split"). Any
# additional args (e.g. --all-emails, for a second independent consumer of
# the same mailbox -- see scripts/daily_update.py's docstring) are passed
# straight through to daily_update.py.
#
# Config comes from ops/daily-update.env (copy daily-update.env.example and
# edit it for this machine) or environment overrides.
set -euo pipefail

MODE="${1:?Usage: run_daily_update.sh <tips-only|daily-price|intraday-price> [extra args]}"
shift

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive:eodhd/prod.db}"
WORK_DIR="${WORK_DIR:-/var/tmp/eodhd-daily-update}"

if [ -f "${REPO_DIR}/ops/daily-update.env" ]; then
    # shellcheck disable=SC1091
    source "${REPO_DIR}/ops/daily-update.env"
fi

# Applied after sourcing daily-update.env so a WORK_DIR set there still gets
# a mode-specific subdirectory -- keeps the three schedules' scratch space
# (and the leftover-recovery check below) from colliding if runs ever
# overlap (e.g. a big Saturday backlog still running into Sunday).
WORK_DIR="${WORK_DIR}/${MODE}"

DB_PATH="${WORK_DIR}/prod.db"
EML_DIR="${WORK_DIR}/eml"

mkdir -p "${WORK_DIR}" "${EML_DIR}"

# If a previous run got as far as updating the local db but failed to push
# it back (network blip, OneDrive hiccup, etc.), it deliberately left the
# local copy in place rather than deleting it. Recover that first, so a
# transient failure never silently loses a night's tips/prices.
if [ -f "${DB_PATH}" ]; then
    echo "[daily-update] found a local db left over from a previous run that never made it back to ${RCLONE_REMOTE} -- retrying that push first"
    rclone copyto "${DB_PATH}" "${RCLONE_REMOTE}"
    echo "[daily-update] recovered previous run's data"
    rm -rf "${WORK_DIR}"
    mkdir -p "${WORK_DIR}" "${EML_DIR}"
fi

echo "[daily-update] pulling db from ${RCLONE_REMOTE}"
if rclone lsf "${RCLONE_REMOTE}" >/dev/null 2>&1; then
    rclone copyto "${RCLONE_REMOTE}" "${DB_PATH}"
else
    echo "[daily-update] no existing remote db found, starting fresh"
fi

"${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/daily_update.py" \
    --db-path "${DB_PATH}" --eml-dir "${EML_DIR}" --mode "${MODE}" "$@"

echo "[daily-update] pushing db back to ${RCLONE_REMOTE}"
rclone copyto "${DB_PATH}" "${RCLONE_REMOTE}"

echo "[daily-update] cleaning up local scratch copy"
rm -rf "${WORK_DIR}"

echo "[daily-update] done"
