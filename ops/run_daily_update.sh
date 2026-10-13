#!/usr/bin/env bash
# Wrapper for the systemd service: keeps this VM stateless with respect to
# the database. Pulls the canonical db down from remote storage (rclone),
# runs the update, pushes it back, then deletes the local scratch copy.
#
# Usage: run_daily_update.sh <tips-only|daily-price|intraday-price|backfill> [extra daily_update.py args, e.g. --all-emails]
# The mode is baked into each .service unit's ExecStart= -- see
# doc/DESIGN_DECISIONS.md ("weekday/Saturday/Sunday split"). Any additional
# args (e.g. --all-emails, for a second independent consumer of the same
# mailbox -- see scripts/daily_update.py's docstring) are passed straight
# through to daily_update.py.
#
# Config comes from ops/daily-update.env (copy daily-update.env.example and
# edit it for this machine) or environment overrides.
#
# After a successful intraday-price run, also takes a dated snapshot of
# the pushed db on the remote and prunes snapshots older than
# SNAPSHOT_RETENTION_DAYS -- see snapshot_and_prune() below and
# doc/DESIGN_DECISIONS.md ("weekly db snapshots + retention").
#
# backfill mode gets its --start-date/--end-date computed here, not baked
# into the .service unit, since the date range advances every run -- see
# the backfill block below and doc/DESIGN_DECISIONS.md ("historical tip
# backfill").
#
# Requires jq (in addition to rclone) -- used by safe_push()'s remote
# staleness check and snapshot_and_prune()'s pruning logic below.
set -euo pipefail

MODE="${1:?Usage: run_daily_update.sh <tips-only|daily-price|intraday-price|backfill> [extra args]}"
shift

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${REPO_DIR}/ops/lib_rclone_sync.sh"

RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive:eodhd/prod.db}"
WORK_DIR="${WORK_DIR:-/var/tmp/eodhd-daily-update}"

# How long a weekly dated snapshot (see snapshot_and_prune() below) is kept
# before being pruned.
SNAPSHOT_RETENTION_DAYS="${SNAPSHOT_RETENTION_DAYS:-28}"

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

# backfill-only config: how many calendar days each batch covers (14 ->
# ~10 weekdays, since the newsletter only sends on weekdays and a calendar
# window just has nothing to find on a weekend -- no separate
# weekday-skipping logic needed), the historical gap being closed, and
# where progress across repeated runs is tracked. All overridable via
# ops/daily-update.env so the batch size can be widened/narrowed mid-run
# without a code change. See doc/DESIGN_DECISIONS.md ("historical tip
# backfill").
BACKFILL_BATCH_DAYS="${BACKFILL_BATCH_DAYS:-14}"
BACKFILL_GAP_START="${BACKFILL_GAP_START:-2026-04-08}"
BACKFILL_GAP_END="${BACKFILL_GAP_END:-2026-08-04}"
BACKFILL_STATE_FILE="${BACKFILL_STATE_FILE:-${REPO_DIR}/ops/backfill_state}"

if [ "${MODE}" = "backfill" ]; then
    if [ -f "${BACKFILL_STATE_FILE}" ]; then
        BACKFILL_NEXT_START="$(cat "${BACKFILL_STATE_FILE}")"
    else
        BACKFILL_NEXT_START="${BACKFILL_GAP_START}"
    fi

    # Plain lexicographic string comparison, not date arithmetic -- safe
    # for fixed-width ISO "YYYY-MM-DD" strings on both bash 3.2 (macOS) and
    # bash 4+ (Linux), so no portability concern here unlike the date *math*
    # below (which does go through Python, same reasoning as
    # snapshot_and_prune()'s date handling).
    if [[ "${BACKFILL_NEXT_START}" > "${BACKFILL_GAP_END}" ]]; then
        echo "[daily-update] backfill complete (${BACKFILL_GAP_START}..${BACKFILL_GAP_END} already covered) -- nothing to do. Disable eodhd-tip-backfill.timer once you're satisfied from the logs."
        exit 0
    fi

    BACKFILL_END="$("${REPO_DIR}/.venv/bin/python" -c "
import datetime, sys
start = datetime.date.fromisoformat(sys.argv[1])
batch_days = int(sys.argv[2])
gap_end = datetime.date.fromisoformat(sys.argv[3])
end = min(start + datetime.timedelta(days=batch_days - 1), gap_end)
print(end.isoformat())
" "${BACKFILL_NEXT_START}" "${BACKFILL_BATCH_DAYS}" "${BACKFILL_GAP_END}")"

    echo "[daily-update] backfill batch: ${BACKFILL_NEXT_START}..${BACKFILL_END}"
    set -- --start-date "${BACKFILL_NEXT_START}" --end-date "${BACKFILL_END}" "$@"
fi

# remote_modtime()/safe_push()/recover_leftover_if_any()/pull_db() all
# come from ops/lib_rclone_sync.sh, sourced above -- shared with
# ops/run_ig_margins.sh.

# Keeps a dated snapshot of ${RCLONE_REMOTE} next to it on the remote
# (e.g. sda.db -> sda.2026-10-11.db), and prunes snapshots older than
# ${SNAPSHOT_RETENTION_DAYS} days. Only called after intraday-price (see
# the mode check at the bottom of this script) -- "after each Sunday run"
# in doc/DESIGN_DECISIONS.md ("weekly db snapshots + retention").
#
# The copy is between two paths on the same rclone remote, so rclone does
# it server-side on OneDrive -- no multi-hundred-MB round trip through
# this VM. Driven entirely by ${RCLONE_REMOTE} (already per-machine), so
# the same convention applies automatically wherever this script runs
# (mars today, a future Mac remote tomorrow) with no machine-specific code.
#
# A failure here is logged and swallowed, not fatal -- unlike safe_push(),
# a missed snapshot/prune this week is recoverable next week, not
# destructive.
snapshot_and_prune() {
    local remote_dir remote_file base ext today snapshot_name snapshot_path
    remote_dir="${RCLONE_REMOTE%/*}"
    remote_file="${RCLONE_REMOTE##*/}"
    ext="${remote_file##*.}"
    base="${remote_file%.*}"
    today="$(date +%F)"
    snapshot_name="${base}.${today}.${ext}"
    snapshot_path="${remote_dir}/${snapshot_name}"

    echo "[daily-update] snapshotting ${RCLONE_REMOTE} -> ${snapshot_path}"
    if ! rclone copyto "${RCLONE_REMOTE}" "${snapshot_path}"; then
        echo "[daily-update] WARNING: snapshot copy failed -- continuing (not fatal, will retry next week)" >&2
        return
    fi

    echo "[daily-update] pruning snapshots older than ${SNAPSHOT_RETENTION_DAYS} days in ${remote_dir}"
    local listing
    if ! listing="$(rclone lsjson "${remote_dir}" 2>/dev/null)"; then
        echo "[daily-update] WARNING: could not list ${remote_dir} for pruning -- skipping" >&2
        return
    fi
    local name date_part
    while IFS= read -r name; do
        [ -z "${name}" ] && continue
        # Only a filename that's exactly <base>.<yyyy-mm-dd>.<ext> is ever
        # a candidate -- a file renamed to anything else (even if it still
        # has an old date in it) is deliberately left alone.
        if [[ "${name}" =~ ^${base}\.([0-9]{4}-[0-9]{2}-[0-9]{2})\.${ext}$ ]]; then
            date_part="${BASH_REMATCH[1]}"
            # Age check via Python's date math (not `date -d`/`date -v`,
            # which differ between GNU/Linux and BSD/macOS -- this script
            # runs on both) using the venv Python this script already
            # requires.
            if ! "${REPO_DIR}/.venv/bin/python" -c "
import datetime, sys
d = datetime.date.fromisoformat(sys.argv[1])
sys.exit(0 if (datetime.date.today() - d).days > int(sys.argv[2]) else 1)
" "${date_part}" "${SNAPSHOT_RETENTION_DAYS}"; then
                continue
            fi
            echo "[daily-update] deleting old snapshot ${name}"
            rclone deletefile "${remote_dir}/${name}" || echo "[daily-update] WARNING: failed to delete ${name}" >&2
        fi
    done < <(echo "${listing}" | jq -r '.[].Name')
}

# If a previous run got as far as updating the local db but failed to push
# it back (network blip, OneDrive hiccup, etc.), it deliberately left the
# local copy in place rather than deleting it. Recover that first, so a
# transient failure never silently loses a night's tips/prices -- but only
# once safe_push confirms nothing else has moved the remote on since.
# recover_leftover_if_any() recreates WORK_DIR but not EML_DIR, which this
# mode needs too.
recover_leftover_if_any
mkdir -p "${EML_DIR}"

pull_db

"${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/scripts/daily_update.py" \
    --db-path "${DB_PATH}" --eml-dir "${EML_DIR}" --mode "${MODE}" "$@"

echo "[daily-update] pushing db back to ${RCLONE_REMOTE}"
safe_push

if [ "${MODE}" = "backfill" ]; then
    # Only advance progress once the push above actually succeeded (set -e
    # would have aborted the script before this line otherwise) -- a crash
    # mid-batch just retries the same window next run, same idempotent
    # spirit as the rest of this script.
    BACKFILL_NEXT_START="$("${REPO_DIR}/.venv/bin/python" -c "
import datetime, sys
end = datetime.date.fromisoformat(sys.argv[1])
print((end + datetime.timedelta(days=1)).isoformat())
" "${BACKFILL_END}")"
    echo "${BACKFILL_NEXT_START}" > "${BACKFILL_STATE_FILE}"
    echo "[daily-update] backfill progress advanced to ${BACKFILL_NEXT_START}"
fi

if [ "${MODE}" = "intraday-price" ]; then
    snapshot_and_prune
fi

echo "[daily-update] cleaning up local scratch copy"
rm -rf "${WORK_DIR}"

echo "[daily-update] done"
