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
#
# Requires jq (in addition to rclone) -- used by safe_push()'s remote
# staleness check below.
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
PULLED_MODTIME_FILE="${DB_PATH}.pulled_modtime"

mkdir -p "${WORK_DIR}" "${EML_DIR}"

# ${RCLONE_REMOTE}'s rclone ModTime right now, "NONE" if it doesn't exist
# yet (a legitimate state -- the very first run, before any db has ever
# been pushed), or "UNKNOWN" if rclone couldn't be reached at all. (rclone
# hashsum comes back empty against this OneDrive remote for a
# recently-modified file -- not computed server-side yet -- so ModTime is
# the reliable signal here, not a hash.)
#
# rclone exit codes 3 ("Directory not found") and 4 ("File not found") are
# its own documented codes for "doesn't exist" -- distinct from any other
# nonzero exit, which means a real problem (bad remote config, network
# down) and must NOT be treated as "safe to start fresh". The assignment
# below is deliberately kept inside the if/else (not a bare `x=$(...)`)
# so a failing `rclone lsjson` doesn't trip `set -e` and abort the script
# before the exit code can even be inspected.
remote_modtime() {
    local json rc
    if json="$(rclone lsjson "${RCLONE_REMOTE}" 2>/dev/null)"; then
        rc=0
    else
        rc=$?
    fi
    if [ "${rc}" -eq 0 ]; then
        if [ -z "${json}" ] || [ "${json}" = "[]" ]; then
            echo "NONE"
        else
            echo "${json}" | jq -r '.[0].ModTime // "NONE"'
        fi
    elif [ "${rc}" -eq 3 ] || [ "${rc}" -eq 4 ]; then
        echo "NONE"
    else
        echo "UNKNOWN"
    fi
}

# Push ${DB_PATH} to ${RCLONE_REMOTE}, but only if the remote is still in
# the state this run pulled it from (recorded in ${PULLED_MODTIME_FILE} at
# pull time below). If a *different* mode's run has pushed in between --
# the gap documented in doc/DESIGN_DECISIONS.md, "Manual recovery
# incident" -- a blind overwrite would silently destroy that run's work.
# This can't merge automatically (that needs table-level knowledge, same
# reason the one actual incident was recovered by hand with an ATTACH-
# based merge, not a script); it only turns the silent overwrite into a
# loud stop, leaving ${DB_PATH} in place for the same kind of manual
# recovery.
safe_push() {
    if [ ! -f "${PULLED_MODTIME_FILE}" ]; then
        echo "[daily-update] ERROR: no record of ${RCLONE_REMOTE}'s state when ${DB_PATH} was pulled (unexpected -- possibly a leftover from before this safety check existed) -- refusing to push automatically. Recover manually: compare ${DB_PATH} against the current remote db and merge by hand (see doc/DESIGN_DECISIONS.md, 'Manual recovery incident')." >&2
        exit 1
    fi
    local pulled_modtime current_modtime
    pulled_modtime="$(cat "${PULLED_MODTIME_FILE}")"
    current_modtime="$(remote_modtime)"
    if [ "${current_modtime}" = "UNKNOWN" ]; then
        echo "[daily-update] ERROR: could not check ${RCLONE_REMOTE}'s current state (rclone lsjson failed) -- refusing to push blindly. Retry once connectivity is confirmed, or recover manually." >&2
        exit 1
    fi
    if [ "${pulled_modtime}" != "${current_modtime}" ]; then
        echo "[daily-update] ERROR: ${RCLONE_REMOTE} has changed since this run pulled it (was '${pulled_modtime}', now '${current_modtime}') -- a different run likely pushed in between. Refusing to blindly overwrite; ${DB_PATH} is left in place for manual merge (see doc/DESIGN_DECISIONS.md, 'Manual recovery incident')." >&2
        exit 1
    fi
    echo "[daily-update] ${RCLONE_REMOTE} unchanged since pull -- safe to push"
    rclone copyto "${DB_PATH}" "${RCLONE_REMOTE}"
}

# If a previous run got as far as updating the local db but failed to push
# it back (network blip, OneDrive hiccup, etc.), it deliberately left the
# local copy in place rather than deleting it. Recover that first, so a
# transient failure never silently loses a night's tips/prices -- but only
# once safe_push confirms nothing else has moved the remote on since.
if [ -f "${DB_PATH}" ]; then
    echo "[daily-update] found a local db left over from a previous run that never made it back to ${RCLONE_REMOTE} -- checking it's still safe to push"
    safe_push
    echo "[daily-update] recovered previous run's data"
    rm -rf "${WORK_DIR}"
    mkdir -p "${WORK_DIR}" "${EML_DIR}"
fi

echo "[daily-update] pulling db from ${RCLONE_REMOTE}"
PULLED_MODTIME="$(remote_modtime)"
if [ "${PULLED_MODTIME}" = "UNKNOWN" ]; then
    echo "[daily-update] ERROR: could not check ${RCLONE_REMOTE}'s current state (rclone lsjson failed) -- aborting rather than guessing." >&2
    exit 1
fi
echo "${PULLED_MODTIME}" > "${PULLED_MODTIME_FILE}"
if [ "${PULLED_MODTIME}" != "NONE" ]; then
    rclone copyto "${RCLONE_REMOTE}" "${DB_PATH}"
else
    echo "[daily-update] no existing remote db found, starting fresh"
fi

"${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/daily_update.py" \
    --db-path "${DB_PATH}" --eml-dir "${EML_DIR}" --mode "${MODE}" "$@"

echo "[daily-update] pushing db back to ${RCLONE_REMOTE}"
safe_push

echo "[daily-update] cleaning up local scratch copy"
rm -rf "${WORK_DIR}"

echo "[daily-update] done"
