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

Every run also logs unresolved_tips() -- tips whose backfill window has
closed but price coverage is still short, usually meaning EODHD stopped
following a ticker through a rename/split/merger and a ticker_aliases entry
is needed -- see _log_unresolved_tips() and doc/DESIGN_DECISIONS.md
("ticker_aliases: smoothing renamed / split / merged / cashed-out tickers").

--mode selects what this run actually fetches -- see doc/DESIGN_DECISIONS.md
("weekday/Saturday/Sunday split") for why price fetching isn't done every
day:
  tips-only       parse new tip emails only, no price fetch (weekdays)
  daily-price     also fetch daily OHLCV into PRICE_TABLENAME (Saturdays)
  intraday-price  also fetch 5m intraday OHLCV into INTRADAY_TABLENAME (Sundays)
Which mode runs which day is entirely a scheduling decision, made in ops/
(three separate systemd timers), not in this script.
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
from eodhd_io import DEFAULT_N2, Database, tips, unresolved_tips  # noqa: E402
from tips_io import parse_tip_emails, tips_exchange2sqlite  # noqa: E402

SENDER_EMAIL = "reports@stockdataanalytics.com"
PRICE_TABLENAME = "daily"
PRICE_INTERVAL = "1d"
INTRADAY_TABLENAME = "intraday_5m"
INTRADAY_INTERVAL = "5m"
TIPS_TABLENAME = "tip_details"  # must match tips_exchange2sqlite's default

# (tablename, interval) for each --mode that actually fetches price data.
# "tips-only" has no entry -- checked explicitly in main() instead.
MODE_PRICE_CONFIG = {
    "daily-price": (PRICE_TABLENAME, PRICE_INTERVAL),
    "intraday-price": (INTRADAY_TABLENAME, INTRADAY_INTERVAL),
}

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


def _all_tips(db: Database, tablename: str) -> list[tuple[str, date]]:
    """All (code, tip_date) pairs ever recorded. Unlike _still_open_tips()
    (recent tips still inside their backfill window), unresolved_tips()
    needs the full history -- it specifically checks tips whose window has
    already closed, which _still_open_tips() would filter out.
    """
    try:
        rows = db.conn.execute(
            f"SELECT DISTINCT code, tip_date FROM {tablename} WHERE code IS NOT NULL"  # noqa: S608
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(code, date.fromisoformat(tip_date)) for code, tip_date in rows]


def _log_unresolved_tips(db: Database) -> None:
    """Surface tips whose backfill window has closed but price coverage is
    still short -- usually means a ticker_aliases entry is needed (see
    doc/DESIGN_DECISIONS.md, "ticker_aliases: smoothing renamed / split /
    merged / cashed-out tickers"). Checked every run so this shows up in
    the log on its own, rather than needing someone to remember to run
    unresolved_tips() manually.
    """
    all_tips = _all_tips(db, TIPS_TABLENAME)
    problems = unresolved_tips(db, all_tips, PRICE_TABLENAME, PRICE_INTERVAL)
    if problems.empty:
        log.info("no unresolved tips")
        return
    log.warning(
        "%d unresolved tip(s) -- window closed but price coverage still short, "
        "likely needs a ticker_aliases entry (see doc/DESIGN_DECISIONS.md): %s",
        len(problems),
        "; ".join(
            f"{row.code} (tip {row.tip_date}, {row.actual_days}d, "
            f"last={row.last_available_date})"
            for row in problems.itertuples()
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--eml-dir", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["tips-only", "daily-price", "intraday-price"],
        help="tips-only: parse emails, no price fetch. daily-price: also "
             "fetch daily OHLCV. intraday-price: also fetch 5m OHLCV.",
    )
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

        if args.mode == "tips-only":
            log.info("tips-only mode: skipping price fetch")
        else:
            # Matches the calendar-day buffer tips() itself uses internally
            # (n2 * 2 + 5) so this cutoff is exactly as generous as the window
            # tips() will actually try to fill.
            cutoff = date.today() - timedelta(days=DEFAULT_N2 * 2 + 5)
            open_tips = _still_open_tips(db, TIPS_TABLENAME, cutoff)
            # Sorted for readable, reproducible logs -- a set union's
            # iteration order is hash-based and scrambles from run to run,
            # which made it impossible to tell from the log which tip ran
            # next when diagnosing a failure (see doc/DESIGN_DECISIONS.md).
            tip_list = sorted(
                set(new_tips) | set(open_tips), key=lambda t: (t[1], t[0])
            )
            tablename, interval = MODE_PRICE_CONFIG[args.mode]

            if tip_list:
                tips(db, tip_list, tablename, interval)
                log.info(
                    "fetched %s price windows for %d tip(s)", interval, len(tip_list)
                )
            else:
                log.info("no tips (new or still-open) to fetch price windows for")

        _log_unresolved_tips(db)


if __name__ == "__main__":
    main()
