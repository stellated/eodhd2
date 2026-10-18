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
It also logs tips whose symbol had no daily bars at all just before
tip_date -- the newsletter tipping a stock under an old ticker -- a week
after the tip rather than once its window closes; see _log_stale_tickers().
And it logs days where a tipped symbol's 5m bars have no volume while its
daily bar traded -- the other way a rename shows up; see _log_dead_intraday().

intraday-price runs also call update_outcomes() after the price fetch --
the outcomes table's entry/exit trigger logic is entirely 5-minute-bar
based, so it has nothing new to work with until intraday_5m does. See
doc/DESIGN_DECISIONS.md ("outcomes design").

--mode selects what this run actually fetches -- see doc/DESIGN_DECISIONS.md
("weekday/Saturday/Sunday split") for why price fetching isn't done every
day:
  tips-only       parse new tip emails only, no price fetch (weekdays)
  daily-price     also fetch daily OHLCV into PRICE_TABLENAME (Saturdays)
  intraday-price  also fetch 5m intraday OHLCV into INTRADAY_TABLENAME (Sundays)
  backfill        one-time historical catch-up for a date range the normal
                   runs never covered -- see below and
                   doc/DESIGN_DECISIONS.md ("historical tip backfill")
Which mode runs which day is entirely a scheduling decision, made in ops/
(three separate systemd timers), not in this script.

--mode backfill (with required --start-date/--end-date, inclusive): parses
only the tip emails within that historical window (read-only IMAP search,
mark_seen=False -- same reasoning as --all-emails below, this must never
touch \\Seen on mail the normal weekday run already processed or still
will), then fetches BOTH daily and 5m price windows and runs
update_outcomes() for exactly the tips parsed -- unlike the other
price-fetching modes, there's no _still_open_tips() cutoff union, since a
tip from months ago is long past its backfill window being "still open".
ops/run_daily_update.sh drives the actual date range from a small state
file, advancing it one batch at a time across repeated runs -- see
doc/DESIGN_DECISIONS.md ("historical tip backfill") for why the batching
itself lives there and not here.

--all-emails: for a second, independent consumer of the same mailbox (e.g. a
Mac running this manually alongside mars's scheduled runs, for an
independent comparison db). \\Seen is shared IMAP-server state, not
per-machine, so this sets BOTH unseen_only=False (search everything, not
just what this machine personally left unread) AND mark_seen=False (never
flip \\Seen -- leave that to mars, the canonical consumer, so it isn't
robbed of "is this new" detection for mail this machine downloads first).
Re-parsing everything every run is slower but harmless: INSERT OR REPLACE
makes it idempotent. See doc/DESIGN_DECISIONS.md ("--all-emails for a
second independent consumer of the same mailbox").
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
from eodhd_io import DEFAULT_N2, Database, tips, unresolved_tips, update_outcomes  # noqa: E402
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

    Checks intraday_5m as well as daily (added 2026-10-14): a tip can be
    fine in daily but never have had its 5m window fetched at all -- the
    2026-08-05..19 gap went unnoticed for weeks that way (see
    doc/DESIGN_DECISIONS.md, "Daily fallback needs EODHD's confirmation,
    not an empty table"). Tips whose outcome was resolved from daily bars
    (fill_granularity='1d' -- EODHD has no intraday for them) are expected
    to be short in intraday_5m, so they're counted, not listed.
    """
    all_tips = _all_tips(db, TIPS_TABLENAME)
    for tablename, interval in (
        (PRICE_TABLENAME, PRICE_INTERVAL),
        (INTRADAY_TABLENAME, INTRADAY_INTERVAL),
    ):
        if all_tips and not db._table_exists(tablename):
            log.info("unresolved-tips check: no %s table yet, skipping", tablename)
            continue
        problems = unresolved_tips(db, all_tips, tablename, interval)
        expected = 0
        if tablename == INTRADAY_TABLENAME and not problems.empty and db._table_exists("outcomes"):
            daily_resolved = {
                (code, date.fromisoformat(tip_date))
                for code, tip_date in db.conn.execute(
                    "SELECT code, tip_date FROM outcomes WHERE fill_granularity = '1d'"
                )
            }
            keep = [(c, d) not in daily_resolved for c, d in zip(problems.code, problems.tip_date)]
            expected = len(problems) - sum(keep)
            problems = problems[keep]
        suffix = (f" ({expected} more resolved from daily bars, as expected)"
                  if expected else "")
        if problems.empty:
            log.info("no unresolved tips in %s%s", tablename, suffix)
            continue
        log.warning(
            "%d unresolved tip(s) in %s -- window closed but price coverage still "
            "short, likely needs a ticker_aliases entry or a re-fetch (see "
            "doc/DESIGN_DECISIONS.md)%s: %s",
            len(problems),
            tablename,
            suffix,
            "; ".join(
                f"{row.code} (tip {row.tip_date}, {row.actual_days}d, "
                f"last={row.last_available_date})"
                for row in problems.itertuples()
            ),
        )


# A stale-ticker tip is only checked once a Saturday daily-price run has had
# the chance to fetch its pre-tip history (the n1 days before are available
# from day 0).
STALE_TICKER_MIN_AGE_DAYS = 7
# How far before tip_date to look for any daily bar under the tipped symbol.
STALE_TICKER_LOOKBACK_DAYS = 14


def _log_stale_tickers(db: Database, today: date | None = None) -> None:
    """Surface tips whose symbol had no daily bars at all in the
    STALE_TICKER_LOOKBACK_DAYS up to tip_date. The newsletter charts a stock
    before tipping it, so no recent history under that symbol means it
    tipped the stock under an old ticker (BLBX->ALOY, ELWS->PDC), not that
    EODHD stopped following it afterwards.

    Unlike _log_unresolved_tips(), this doesn't wait ~45 days for the tip's
    window to close -- it fires a week after the tip. The entry zone is
    logged too: compare it with a candidate successor's close the day
    before tip_date (see doc/TICKER_ALIASES.md, "Lessons").
    """
    if not (db._table_exists(TIPS_TABLENAME) and db._table_exists(PRICE_TABLENAME)):
        return
    today = today or date.today()
    # Already aliased: the next daily-price run fills it in, and
    # _log_unresolved_tips() catches it if the alias doesn't help.
    aliased = (
        "AND NOT EXISTS (SELECT 1 FROM ticker_aliases a "
        "WHERE a.old_code = t.code AND a.effective_date <= t.tip_date)"
        if db._table_exists("ticker_aliases") else ""
    )
    rows = db.conn.execute(
        f"""
        SELECT t.code, t.tip_date, MIN(t.entry_zone_low), MAX(t.entry_zone_high)
        FROM {TIPS_TABLENAME} t
        WHERE t.code IS NOT NULL AND t.tip_date <= ?
          AND NOT EXISTS (
              SELECT 1 FROM {PRICE_TABLENAME} d
              WHERE d.code = t.code
                AND d.date BETWEEN date(t.tip_date, ?) AND t.tip_date)
          {aliased}
        GROUP BY t.code, t.tip_date
        ORDER BY t.tip_date, t.code
        """,  # noqa: S608
        ((today - timedelta(days=STALE_TICKER_MIN_AGE_DAYS)).isoformat(),
         f"-{STALE_TICKER_LOOKBACK_DAYS} days"),
    ).fetchall()
    if not rows:
        log.info("no stale-ticker tips in %s", PRICE_TABLENAME)
        return
    log.warning(
        "%d tip(s) with no %s bars in the %d days before tip_date -- likely tipped "
        "under an old ticker; find the new symbol and add a ticker_aliases entry "
        "(see doc/TICKER_ALIASES.md): %s",
        len(rows),
        PRICE_TABLENAME,
        STALE_TICKER_LOOKBACK_DAYS,
        "; ".join(f"{code} (tip {tip_date}, entry {low}-{high})"
                  for code, tip_date, low, high in rows),
    )


# A day of 5m bars with no volume at all only counts as "dead" when the
# daily bar shows real trading -- thin tickers (MKDW, RDAC: a few hundred
# shares a day) legitimately have whole days of empty 5m bars.
DEAD_INTRADAY_MIN_DAILY_VO = 5000
_SESSION_SECONDS = 6 * 3600 + 30 * 60


def _log_dead_intraday(db: Database) -> None:
    """Surface days, inside any tip's window, where intraday_5m has bars but
    none with volume while the daily bar traded at least
    DEAD_INTRADAY_MIN_DAILY_VO shares. That's what a rename looks like at
    EODHD: it keeps serving the old symbol's daily bars for weeks while its
    5m bars go empty (SBLX->FABC, RVYL->RTB), so neither
    _log_unresolved_tips() (it counts days present, empty or not) nor
    _log_stale_tickers() (daily only) catches it. Bars with no volume never
    trigger an outcome, so a tip on such days resolves wrongly or not at
    all. Fixed by an alias plus a re-fetch, after which the days have real
    volume and drop out of this check on their own.
    """
    tables = (TIPS_TABLENAME, PRICE_TABLENAME, INTRADAY_TABLENAME)
    if not all(db._table_exists(t) for t in tables):
        return
    rows = db.conn.execute(
        f"""
        SELECT d.code, d.date, GROUP_CONCAT(DISTINCT t.tip_date)
        FROM {PRICE_TABLENAME} d
        JOIN (SELECT DISTINCT code, tip_date FROM {TIPS_TABLENAME}) t
          ON t.code = d.code
         AND d.date BETWEEN t.tip_date AND date(t.tip_date, ?)
        WHERE d.vo >= ?
          AND EXISTS (SELECT 1 FROM {INTRADAY_TABLENAME} i WHERE i.code = d.code
                      AND i.timestamp >= d.timestamp AND i.timestamp < d.timestamp + ?)
          AND NOT EXISTS (SELECT 1 FROM {INTRADAY_TABLENAME} i WHERE i.code = d.code
                          AND i.timestamp >= d.timestamp AND i.timestamp < d.timestamp + ?
                          AND i.vo > 0)
        GROUP BY d.code, d.date
        ORDER BY d.code, d.date
        """,  # noqa: S608
        (f"+{DEFAULT_N2 * 2 + 5} days", DEAD_INTRADAY_MIN_DAILY_VO,
         _SESSION_SECONDS, _SESSION_SECONDS),
    ).fetchall()
    if not rows:
        log.info("no dead %s days in tip windows", INTRADAY_TABLENAME)
        return
    by_code: dict[str, list] = {}
    for code, day, tip_dates in rows:
        by_code.setdefault(code, []).append((day, tip_dates.split(",")))
    log.warning(
        "%d ticker(s) with %s days that have no volume while %s traded -- likely "
        "renamed (EODHD's 5m feed for the old symbol goes empty first); find the "
        "new symbol, add a ticker_aliases entry and re-fetch (see "
        "doc/TICKER_ALIASES.md): %s",
        len(by_code),
        INTRADAY_TABLENAME,
        PRICE_TABLENAME,
        "; ".join(
            f"{code} ({len(days)}d, {days[0][0]}..{days[-1][0]}, tips "
            f"{','.join(sorted({t for _, ts in days for t in ts}))})"
            for code, days in by_code.items()
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--eml-dir", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["tips-only", "daily-price", "intraday-price", "backfill"],
        help="tips-only: parse emails, no price fetch. daily-price: also "
             "fetch daily OHLCV. intraday-price: also fetch 5m OHLCV. "
             "backfill: historical catch-up for --start-date..--end-date.",
    )
    parser.add_argument(
        "--all-emails",
        action="store_true",
        help="For a second independent consumer of the same mailbox (see "
             "module docstring): re-search/re-parse every matching email "
             "AND never mark one \\Seen, so this run never interferes with "
             "the other consumer's unseen-mail detection either. Leave "
             "unset for the normal unseen_only, mark-as-read run.",
    )
    parser.add_argument(
        "--start-date", type=date.fromisoformat,
        help="--mode backfill only: inclusive start of the historical "
             "tip-email window to parse and fetch.",
    )
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        help="--mode backfill only: inclusive end of the historical "
             "tip-email window to parse and fetch.",
    )
    args = parser.parse_args()

    if args.mode == "backfill":
        if args.start_date is None or args.end_date is None:
            parser.error("--mode backfill requires both --start-date and --end-date")
        if args.all_emails:
            parser.error("--all-emails is not meaningful with --mode backfill "
                         "(which already uses its own date-ranged, mark_seen=False search)")
    elif args.start_date is not None or args.end_date is not None:
        parser.error("--start-date/--end-date are only valid with --mode backfill")

    load_dotenv()
    args.eml_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "backfill":
        download_emails(
            imap_server=os.environ["imap_server"],
            username=os.environ["imap_username"],
            password=os.environ["imap_password"],
            target_folder=args.eml_dir,
            sender_email=SENDER_EMAIL,
            unseen_only=False,
            mark_seen=False,
            since_date=args.start_date,
            before_date=args.end_date + timedelta(days=1),
        )
    else:
        download_emails(
            imap_server=os.environ["imap_server"],
            username=os.environ["imap_username"],
            password=os.environ["imap_password"],
            target_folder=args.eml_dir,
            sender_email=SENDER_EMAIL,
            unseen_only=not args.all_emails,
            mark_seen=not args.all_emails,
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
        elif args.mode == "backfill":
            # No _still_open_tips() cutoff union here -- every tip in this
            # window is months past its backfill window being "still open".
            # Fetch exactly what was just parsed, for both price tables,
            # then run outcomes -- a one-time catch-up, not the regular
            # weekly cadence, so it does both instead of picking one table
            # per mode like the other branches.
            tip_list = sorted(set(new_tips), key=lambda t: (t[1], t[0]))
            if tip_list:
                tips(db, tip_list, PRICE_TABLENAME, PRICE_INTERVAL)
                log.info("fetched %s price windows for %d backfilled tip(s)",
                          PRICE_INTERVAL, len(tip_list))
                tips(db, tip_list, INTRADAY_TABLENAME, INTRADAY_INTERVAL)
                log.info("fetched %s price windows for %d backfilled tip(s)",
                          INTRADAY_INTERVAL, len(tip_list))
                update_outcomes(db)
                log.info("updated outcomes")
            else:
                log.info("no tips parsed in %s..%s -- nothing to fetch",
                          args.start_date, args.end_date)
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

            if args.mode == "intraday-price":
                update_outcomes(db)
                log.info("updated outcomes")

        _log_unresolved_tips(db)
        _log_stale_tickers(db)
        _log_dead_intraday(db)


if __name__ == "__main__":
    main()
