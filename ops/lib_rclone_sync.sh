# Shared by ops/run_daily_update.sh and ops/run_ig_margins.sh -- both pull
# a canonical db from remote storage, run something against it, and push
# it back, so the pull/push/staleness-check mechanism is identical for
# both rather than duplicated. See doc/DESIGN_DECISIONS.md ("IG tiered
# margins") for why a second wrapper needed this at all.
#
# Callers must set RCLONE_REMOTE, DB_PATH, and PULLED_MODTIME_FILE before
# sourcing/calling these -- they're read from the caller's scope, not
# passed as parameters, to keep call sites unchanged from before this was
# split out.

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

# ${RCLONE_REMOTE}'s size in bytes right now, or "UNKNOWN" if it can't be
# read. Used to check a pull or push transferred the whole file.
remote_size() {
    local json
    if json="$(rclone lsjson "${RCLONE_REMOTE}" 2>/dev/null)"; then
        echo "${json}" | jq -r '.[0].Size // "UNKNOWN"'
    else
        echo "UNKNOWN"
    fi
}

# Size of file $1 in bytes, or "missing". wc rather than stat -c, which is
# GNU-only (these scripts also run on the Mac).
file_size() {
    if [ -f "$1" ]; then wc -c < "$1" | tr -d ' '; else echo "missing"; fi
}

# True if $1 is a non-empty file starting with SQLite's 16-byte header.
# On 2026-11-12 a failed pull left OneDrive's 585-byte error body in place
# of the db, and every later run pushed it back as if it were a real
# result (see doc/DESIGN_DECISIONS.md, "sda.db replaced by an error page").
# This doesn't prove the db is intact (PRAGMA quick_check takes minutes on
# 2 GB), only that it's a SQLite file at all, which is what went wrong.
is_sqlite_file() {
    [ -s "$1" ] && head -c 16 "$1" | cmp -s - <(printf 'SQLite format 3\0')
}

# Push ${DB_PATH} to ${RCLONE_REMOTE}, but only if the remote is still in
# the state this run pulled it from (recorded in ${PULLED_MODTIME_FILE} at
# pull time). If a *different* run has pushed in between -- the gap
# documented in doc/DESIGN_DECISIONS.md, "Manual recovery incident" -- a
# blind overwrite would silently destroy that run's work. This can't merge
# automatically (that needs table-level knowledge, same reason the one
# actual incident was recovered by hand with an ATTACH-based merge, not a
# script); it only turns the silent overwrite into a loud stop, leaving
# ${DB_PATH} in place for the same kind of manual recovery.
safe_push() {
    # CHANGED: never push something that isn't a SQLite db, whatever the
    # ModTime check says (2026-11-12: the junk pushed was "unchanged since
    # pull" every time).
    if ! is_sqlite_file "${DB_PATH}"; then
        echo "[daily-update] ERROR: ${DB_PATH} is not a SQLite db ($(file_size "${DB_PATH}") bytes) -- refusing to push it to ${RCLONE_REMOTE}. Move it aside and check the remote by hand." >&2
        exit 1
    fi
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
    # CHANGED: rclone retries through OneDrive errors (a 503 mid-upload on
    # 2026-11-19), so confirm the remote ended up the size we sent.
    local local_size pushed_size
    local_size="$(file_size "${DB_PATH}")"
    pushed_size="$(remote_size)"
    if [ "${pushed_size}" != "${local_size}" ]; then
        echo "[daily-update] ERROR: pushed ${local_size} bytes but ${RCLONE_REMOTE} is now ${pushed_size} bytes -- ${DB_PATH} left in place; check the remote by hand." >&2
        exit 1
    fi
}

# If a previous run got as far as updating the local db but failed to push
# it back (network blip, OneDrive hiccup, etc.), it deliberately left the
# local copy in place rather than deleting it. Recover that first, so a
# transient failure never silently loses progress -- but only once
# safe_push confirms nothing else has moved the remote on since. Callers
# invoke this before pulling; it's a function (not inlined at each call
# site) purely so both wrapper scripts share one copy of the recovery
# logging, not because it needs to be called more than once per run.
recover_leftover_if_any() {
    if [ -f "${DB_PATH}" ]; then
        echo "[daily-update] found a local db left over from a previous run that never made it back to ${RCLONE_REMOTE} -- checking it's still safe to push"
        safe_push
        echo "[daily-update] recovered previous run's data"
        rm -rf "${WORK_DIR}"
        mkdir -p "${WORK_DIR}"
    fi
}

# Pulls ${RCLONE_REMOTE} down to ${DB_PATH} if it exists, recording its
# ModTime into ${PULLED_MODTIME_FILE} for safe_push()'s later staleness
# check either way.
pull_db() {
    echo "[daily-update] pulling db from ${RCLONE_REMOTE}"
    local pulled_modtime
    pulled_modtime="$(remote_modtime)"
    if [ "${pulled_modtime}" = "UNKNOWN" ]; then
        echo "[daily-update] ERROR: could not check ${RCLONE_REMOTE}'s current state (rclone lsjson failed) -- aborting rather than guessing." >&2
        exit 1
    fi
    echo "${pulled_modtime}" > "${PULLED_MODTIME_FILE}"
    if [ "${pulled_modtime}" != "NONE" ]; then
        # CHANGED: download to a .partial file and only move it to
        # ${DB_PATH} once it's whole and a SQLite db. A file at ${DB_PATH}
        # means "this run's work, not yet pushed" to the next run's
        # recover_leftover_if_any(), so a failed or junk download must
        # never be left there (2026-11-12).
        local partial="${DB_PATH}.partial" expected_size
        expected_size="$(remote_size)"
        rm -f "${partial}"
        if ! rclone copyto "${RCLONE_REMOTE}" "${partial}"; then
            rm -f "${partial}" "${PULLED_MODTIME_FILE}"
            echo "[daily-update] ERROR: pull of ${RCLONE_REMOTE} failed -- nothing left behind; the next run pulls again." >&2
            exit 1
        fi
        if [ "$(file_size "${partial}")" != "${expected_size}" ] || ! is_sqlite_file "${partial}"; then
            echo "[daily-update] ERROR: pulled ${RCLONE_REMOTE} is $(file_size "${partial}") bytes (remote says ${expected_size}) and/or not a SQLite db -- discarding it. If the remote itself is bad, restore it from a snapshot or OneDrive's version history." >&2
            rm -f "${partial}" "${PULLED_MODTIME_FILE}"
            exit 1
        fi
        mv "${partial}" "${DB_PATH}"
    else
        echo "[daily-update] no existing remote db found, starting fresh"
    fi
}
