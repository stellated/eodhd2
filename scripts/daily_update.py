#!/usr/bin/env python3
"""Nightly job: download new tip emails, parse them into the tips tables, and
fetch OHLCV price windows around each tip.

This is meant to run against a scratch copy of the database that a wrapper
script syncs down from remote storage before this runs and back up
afterwards — see ops/run_daily_update.sh. It never decides where the
canonical database lives; it just reads/writes whatever --db-path points at.

A tip's price window (n1 trading days before, n2 after) can't be filled in
by a single run: the newsletter arrives before market open, so the first
fetch for a brand-new tip only ever gets the n1 days *before* it -- the n2
days *after* don't exist in EODHD yet. To handle this, every run also
re-requests price windows for any recent tip that might still be missing
post-tip-date days, not just ones parsed from today's new email -- see
_still_open_tips() and doc/DESIGN_DECISIONS.md ("daily_update.py re-requests
still-open tips").
"""
import argparse
import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from email_downloader import download_emails  # noqa: E402
from eodhd_io import DEFAULT_N2, Database, tips  # noqa: E402
from tips_io import parse_tip_emails, tips_exchange2sqlite  # noqa: E402

SENDER_EMAIL = "reports@stockdataanalytics.com"
PRICE_TABLENAME = "daily"
PRICE_INTERVAL = "1d"
TIPS_TABLENAME = "tip_details"  # must match tips_exchange2sqlite's default

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daily_update")


def _still_open_tips(
    db: Database, tablename: str, cutoff: date
) -> list[tuple[str, date]]:
    """Return (code, tip_date) for every tip on or after `cutoff`.

    Used as a cheap calendar-day proxy for "this tip's post-tip-date price
    window might not be fully backfilled yet" -- consistent with the wide
    window/trim-after approach tips() itself already uses for the same
    weekend/half-day problem, rather than inspecting the price table for
    actual coverage (which would need to reason about padded rows -- see
    doc/DESIGN_DECISIONS.md). Re-including a tip whose window is already
    complete is harmless: tips() is idempotent, so it's just a wasted
    re-fetch.
    """
    try:
        rows = db.conn.execute(
            f"SELECT DISTINCT code, tip_date FROM {tablename} "  # noqa: S608
            f"WHERE tip_date >= ? AND code IS NOT NULL",
            (cutoff.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # tip_details doesn't exist yet (e.g. very first run)
    return [(code, date.fromisoformat(tip_date)) for code, tip_date in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--eml-dir", required=True, type=Path)
    args = parser.parse_args()

    load_dotenv()
    args.eml_dir.mkdir(parents=True, exist_ok=True)

    download_emails(
        imap_server=os.environ["imap_server"],
        username=os.environ["imap_username"],
        password=os.environ["imap_password"],
        target_folder=args.eml_dir,
        sender_email=SENDER_EMAIL,
        unseen_only=True,
    )

    eml_files = sorted(args.eml_dir.glob("*.eml"))

    with Database(args.db_path, api_token=os.environ["EODHD_API_TOKEN"]) as db:
        new_tips = []
        if eml_files:
            exchange_pdf, tips_pdf = parse_tip_emails(eml_files)
            tips_exchange2sqlite(exchange_pdf, tips_pdf, db, tips_tablename=TIPS_TABLENAME)
            log.info("parsed %d tip(s) from %d email(s)", len(tips_pdf), len(eml_files))
            new_tips = list(zip(tips_pdf["code"], tips_pdf["tip_date"]))
        else:
            log.info("no new tip emails since last run")

        # Matches the calendar-day buffer tips() itself uses internally
        # (n2 * 2 + 5) so this cutoff is exactly as generous as the window
        # tips() will actually try to fill.
        cutoff = date.today() - timedelta(days=DEFAULT_N2 * 2 + 5)
        open_tips = _still_open_tips(db, TIPS_TABLENAME, cutoff)
        # Sorted for readable, reproducible logs -- a set union's iteration
        # order is hash-based and scrambles from run to run, which made it
        # impossible to tell from the log which tip ran next when diagnosing
        # a failure (see doc/DESIGN_DECISIONS.md).
        tip_list = sorted(set(new_tips) | set(open_tips), key=lambda t: (t[1], t[0]))

        if not tip_list:
            log.info("no tips (new or still-open) to fetch price windows for")
            return

        tips(db, tip_list, PRICE_TABLENAME, PRICE_INTERVAL)
        log.info("fetched price windows for %d tip(s)", len(tip_list))


if __name__ == "__main__":
    main()
