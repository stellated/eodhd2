#!/usr/bin/env python3
"""Fetch EODHD daily bars for Alphanume's Optionable Tickers universe into
universe.db (qgmi's data request, row 3 and 4: every ticker that was ever
in a monthly snapshot, plus QGMI's own picks, SPY and the 11 SPDR sector
ETFs).

universe.db is separate from sda.db (Ian, 2026-10-29): it isn't tied to
the tips, and sda.db is pulled and pushed every weekday. Like alphanume.db
it lives on mars and is copied to OneDrive afterwards by hand; nothing
else writes it.

Each ticker is fetched once, from LOOKBACK_DAYS before its first
appearance (13 months of momentum lookback) to --end, through
ticker_aliases (copied into universe.db from sda.db with --aliases-from)
so a renamed ticker's series continues under its old symbol. The result
of every ticker goes in `fetch_status`, so a rerun skips tickers already
done and the extract can list gaps rather than drop them (survivorship).
One request a ticker, whatever the range: 1 EODHD call each.

  get_universe_daily.py --alphanume-db ~/data/alphanume.db \\
      --db-path ~/data/universe.db --aliases-from sda.db [--limit N]
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from eodhd_io import Database, _fetch_daily_resolved, pandas2sqlite  # noqa: E402

TABLENAME = "daily"
LOOKBACK_DAYS = 400  # 13 months, plus a margin for holidays
BENCHMARKS = ["SPY", "XLK", "XLF", "XLV", "XLE", "XLU", "XLY", "XLP", "XLI", "XLB",
              "XLRE", "XLC"]
BENCHMARK_START = date(2017, 12, 1)  # 13 months before the first snapshot (2019-01)

_DDL_STATUS = """
CREATE TABLE IF NOT EXISTS fetch_status (
    code TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    rows INTEGER,
    first_date TEXT,
    last_date TEXT,
    error TEXT,
    fetched_at TEXT NOT NULL
)
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("get_universe_daily")


def eodhd_code(ticker: str) -> str:
    """Alphanume's 'BRK.B' is EODHD's 'BRK-B.US' (as in eodhd_budget.py)."""
    return f"{ticker.strip().upper().replace('.', '-')}.US"


def first_appearances(alphanume_conn: sqlite3.Connection) -> dict[str, date]:
    """Ticker -> earliest date it appears in an Optionable Tickers snapshot
    or a QGMI pick (6 picks were never in Optionable Tickers)."""
    first: dict[str, date] = {}
    rows = alphanume_conn.execute(
        "SELECT r.body FROM raw_responses r JOIN windows w ON w.id = r.window_id "
        "WHERE w.job IN ('optionable', 'qgmi') AND w.capture = 'history' "
        "AND w.completed_at IS NOT NULL AND w.superseded = 0"
    )
    for (body,) in rows:
        for row in json.loads(body)["data"]:
            d = date.fromisoformat(row["date"])
            t = row["ticker"]
            if t not in first or d < first[t]:
                first[t] = d
    return first


def fetch_plan(first: dict[str, date]) -> list[tuple[str, str, date]]:
    """(ticker, code, from_date) for every universe ticker and benchmark,
    sorted by code."""
    plan = {eodhd_code(t): (t, d - timedelta(days=LOOKBACK_DAYS)) for t, d in first.items()}
    for t in BENCHMARKS:
        code = eodhd_code(t)
        start = min(BENCHMARK_START, plan[code][1]) if code in plan else BENCHMARK_START
        plan[code] = (t, start)
    return sorted((t, code, start) for code, (t, start) in plan.items())


def copy_aliases(db: Database, sda_path: Path) -> int:
    db.conn.execute("ATTACH DATABASE ? AS sda", (f"file:{sda_path}?mode=ro",))
    try:
        sql = db.conn.execute(
            "SELECT sql FROM sda.sqlite_master WHERE name = 'ticker_aliases'"
        ).fetchone()[0]
        db.conn.execute("DROP TABLE IF EXISTS main.ticker_aliases")
        db.conn.execute(sql)
        db.conn.execute("INSERT INTO main.ticker_aliases SELECT * FROM sda.ticker_aliases")
        db.conn.commit()
    finally:
        db.conn.execute("DETACH DATABASE sda")
    return db.conn.execute("SELECT COUNT(*) FROM ticker_aliases").fetchone()[0]


def done(db: Database, code: str) -> bool:
    row = db.conn.execute("SELECT status FROM fetch_status WHERE code = ?", (code,)).fetchone()
    return row is not None and row[0] == "ok"


def record(db: Database, ticker, code, from_date, to_date, status, pdf=None, error=None):
    rows = first_date = last_date = None
    if pdf is not None:
        real = pdf[pdf["vo"] > 0]
        rows = len(pdf)
        if not real.empty:
            first_date, last_date = str(real["date"].min()), str(real["date"].max())
    db.conn.execute(
        "INSERT OR REPLACE INTO fetch_status VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (code, ticker, from_date.isoformat(), to_date.isoformat(), status, rows,
         first_date, last_date, error, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    db.conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alphanume-db", required=True, type=Path)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--aliases-from", type=Path,
                        help="sda.db to copy ticker_aliases from (replaces universe.db's copy)")
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--limit", type=int, help="fetch at most N tickers this run")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ["EODHD_API_TOKEN"]
    alphanume = sqlite3.connect(f"file:{args.alphanume_db}?mode=ro", uri=True)
    plan = fetch_plan(first_appearances(alphanume))
    alphanume.close()

    with Database(args.db_path) as db:
        db.conn.execute(_DDL_STATUS)
        if args.aliases_from:
            log.info("copied %d ticker_aliases rows", copy_aliases(db, args.aliases_from))
        todo = [p for p in plan if not done(db, p[1])]
        if args.limit:
            todo = todo[:args.limit]
        log.info("%d tickers planned, %d to fetch this run", len(plan), len(todo))
        for i, (ticker, code, from_date) in enumerate(todo, 1):
            try:
                pdf = _fetch_daily_resolved(db, code, token, from_date, args.end)
                pandas2sqlite(pdf, db.conn, TABLENAME)
                record(db, ticker, code, from_date, args.end, "ok", pdf=pdf)
            except Exception as e:  # logged and recorded; the extract lists it as a gap
                log.warning("%s: %s: %s", code, type(e).__name__, e)
                record(db, ticker, code, from_date, args.end, "error",
                       error=f"{type(e).__name__}: {e}"[:500])
            if i % 250 == 0:
                log.info("%d/%d done", i, len(todo))
            time.sleep(0.05)
        ok, err = db.conn.execute(
            "SELECT SUM(status = 'ok'), SUM(status = 'error') FROM fetch_status"
        ).fetchone()
        log.info("fetch_status: %s ok, %s error", ok, err)


if __name__ == "__main__":
    main()
