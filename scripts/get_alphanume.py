#!/usr/bin/env python3
"""Capture alphanume API responses, unchanged, into a local SQLite file.

Built for the 3-day trial (see DAProjects/Alphanume Trial Plan.md): the
only job during the trial is to capture every history once, plus a few
live snapshots of what's published each day. Nothing is parsed into proper
tables here; that happens afterwards, from the stored responses.

Subcommands:
  pull JOB [JOB ...] | pull --all   full history for the named jobs (JOBS)
  live JOB [JOB ...]                the last LIVE_LOOKBACK_DAYS, as served now
  summary                           pages, rows, date range, tickers per job

Storage (--db-path, e.g. alphanume.db, kept apart from sda.db):
  windows        one row per request window (job, params, capture
                 'history' or 'live'). completed_at is set once all its
                 pages are stored, so a rerun skips completed windows and
                 an interrupted pull resumes where it stopped.
  raw_responses  every HTTP response, pointing at its window: page, the
                 params actually sent, status, row count, body text.

How a job is split into requests (JOBS[...]["mode"]):
  range       date_gte/date_lte windows of WINDOW_DAYS from HISTORY_START.
              An unpaged endpoint returning NONPAGED_MAX_ROWS or more rows
              may have been cut off silently, so that window is marked
              superseded and refetched as two halves.
  per_ticker  one request per ticker for its whole history (paged), for
              the tickers already captured by the jobs in "tickers_from"
              (market cap etc. are only needed for names the strategies
              actually touch).
  once        a single request with no parameters.
Paged endpoints are followed through next_cursor: each key in it is sent
back as cursor_<key>, as the docs describe for premarket-drop-risk.

Requests are spaced MIN_INTERVAL_S apart (the Pro limit is 600 a minute;
the terms forbid "aggressively" scraping), and 429/5xx responses are
retried with backoff. Needs ALPHANUME_API_KEY in .env.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.alphanume.com/v1"
MIN_INTERVAL_S = 1.0
MAX_RETRIES = 5
HISTORY_START = date(2005, 1, 1)
WINDOW_DAYS = 365
NONPAGED_MAX_ROWS = 1000
LIVE_LOOKBACK_DAYS = 7
NY = ZoneInfo("America/New_York")

_TICKER_JOBS = ["qgmi", "premarket_drop", "reverse_splits", "dilution", "ndm"]
JOBS = {
    "ndm": {"endpoint": "next-day-movers", "mode": "range"},
    "premarket_drop": {"endpoint": "premarket-drop-risk", "mode": "range", "paged": True},
    "qgmi": {"endpoint": "quant-galore-momentum-index", "mode": "range"},
    "optionable": {"endpoint": "optionable-tickers", "mode": "range", "paged": True},
    "reverse_splits": {"endpoint": "capital/reverse-splits", "mode": "range", "paged": True},
    "dilution": {"endpoint": "dilution", "mode": "range"},
    # market cap for the whole optionable universe: QGMI's small-cap question
    "market_cap": {"endpoint": "historical-market-cap", "mode": "per_ticker", "paged": True,
                   "tickers_from": _TICKER_JOBS + ["optionable"]},
    "filing_intensity": {"endpoint": "filing-intensity", "mode": "per_ticker", "paged": True,
                         "tickers_from": _TICKER_JOBS},
    "risk_regime": {"endpoint": "sp500-risk-regime", "mode": "range"},
    "wikipedia_views": {"endpoint": "wikipedia-views", "mode": "per_ticker", "paged": True,
                        "tickers_from": ["qgmi"]},
    "ticker_classification": {"endpoint": "ticker-classification", "mode": "once"},
}

_DDL = """
CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    params TEXT NOT NULL,         -- JSON, sorted keys
    capture TEXT NOT NULL,        -- 'history' or 'live'
    started_at TEXT NOT NULL,     -- UTC ISO
    completed_at TEXT,            -- NULL until every page is stored
    superseded INTEGER NOT NULL DEFAULT 0,  -- 1 = possibly cut off, split
    pages INTEGER,
    row_count INTEGER
);
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    endpoint TEXT NOT NULL,
    page INTEGER NOT NULL,        -- 0 = first page
    page_params TEXT NOT NULL,    -- exactly what was sent, cursors included
    fetched_at TEXT NOT NULL,     -- UTC ISO
    http_status INTEGER NOT NULL,
    row_count INTEGER,
    body TEXT
);
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class AlphanumeError(RuntimeError):
    """A response that retrying won't fix (4xx other than 429, or not the
    expected JSON envelope)."""


class Client:
    """requests wrapper: API key header, spacing between requests, retries."""

    def __init__(self, api_key: str, session=None, sleep=_time.sleep, clock=_time.monotonic):
        self.session = session or requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.sleep, self.clock = sleep, clock
        self._last = None

    def get(self, endpoint: str, params: dict) -> tuple[int, str]:
        for attempt in range(MAX_RETRIES + 1):
            if self._last is not None:
                wait = MIN_INTERVAL_S - (self.clock() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = self.clock()
            resp = self.session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=120)
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt == MAX_RETRIES:
                return resp.status_code, resp.text
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 2)
            logging.warning(f"{endpoint} {params}: HTTP {resp.status_code}, "
                            f"retrying in {delay:.0f}s")
            self.sleep(delay)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")  # live timers and a manual pull may overlap
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(_DDL)
    return conn


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _completed(conn, job: str, params: dict) -> bool:
    return conn.execute(
        "SELECT 1 FROM windows WHERE job = ? AND params = ? AND capture = 'history' "
        "AND completed_at IS NOT NULL AND superseded = 0",
        (job, _key(params)),
    ).fetchone() is not None


def _ticker_completed(conn, job: str, ticker: str) -> bool:
    """A per-ticker job's history window for `ticker` is complete, whatever
    its end date (a later run's window ends on a later day)."""
    return conn.execute(
        "SELECT 1 FROM windows WHERE job = ? AND json_extract(params, '$.ticker') = ? "
        "AND capture = 'history' AND completed_at IS NOT NULL AND superseded = 0",
        (job, ticker),
    ).fetchone() is not None


def fetch_window(conn, client: Client, job: str, params: dict,
                 capture: str = "history") -> tuple[int, int]:
    """Fetch every page of one request window and store each response.
    Returns (window id, total rows) and marks the window complete. Raises
    AlphanumeError on a response retrying won't fix (stored first; the
    window stays incomplete, so a rerun tries it again)."""
    endpoint = JOBS[job]["endpoint"]
    window_id = conn.execute(
        "INSERT INTO windows (job, params, capture, started_at) VALUES (?,?,?,?)",
        (job, _key(params), capture, _now()),
    ).lastrowid
    # commit before the request: an open write transaction held through every
    # HTTP call kept a long pull holding the lock, and the live timers' captures
    # timed out on "database is locked" (2026-10-27)
    conn.commit()
    page, total, page_params = 0, 0, dict(params)
    while True:
        status, body = client.get(endpoint, page_params)
        payload, rows = None, None
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                rows = len(payload["data"])
        except ValueError:
            pass
        conn.execute(
            "INSERT INTO raw_responses (window_id, endpoint, page, page_params, "
            "fetched_at, http_status, row_count, body) VALUES (?,?,?,?,?,?,?,?)",
            (window_id, endpoint, page, _key(page_params), _now(), status, rows, body),
        )
        conn.commit()
        if status != 200 or rows is None:
            raise AlphanumeError(f"{endpoint} {page_params}: HTTP {status}: {body[:300]}")
        total += rows
        cursor = payload.get("next_cursor")
        if not (payload.get("has_more") and isinstance(cursor, dict) and cursor):
            break
        page += 1
        page_params = {**params, **{k if k.startswith("cursor_") else f"cursor_{k}": v
                                    for k, v in cursor.items()}}
    conn.execute(
        "UPDATE windows SET completed_at = ?, pages = ?, row_count = ? WHERE id = ?",
        (_now(), page + 1, total, window_id),
    )
    conn.commit()
    return window_id, total


def _fetch_logged(conn, client, job, params, capture="history") -> tuple[int, int]:
    window_id, rows = fetch_window(conn, client, job, params, capture)
    logging.info(f"{job} {capture} {params}: {rows} rows")
    return window_id, rows


def _range_windows(start: date, end: date, days: Optional[int] = None):
    days = days or WINDOW_DAYS
    while start <= end:
        stop = min(start + timedelta(days=days - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


def pull_range(conn, client, job: str, start: date, end: date,
               extra: Optional[dict] = None, days: Optional[int] = None) -> int:
    """Full history for a date-range job, halving any unpaged window that
    may have been cut off. Returns the rows captured by this call (windows
    completed on an earlier run are skipped, not counted)."""
    paged = JOBS[job].get("paged", False)
    stack = list(_range_windows(start, end, days))[::-1]
    total = 0
    while stack:
        lo, hi = stack.pop()
        params = {**(extra or {}), "date_gte": lo.isoformat(), "date_lte": hi.isoformat()}
        if _completed(conn, job, params):
            continue
        window_id, rows = _fetch_logged(conn, client, job, params)
        if not paged and rows >= NONPAGED_MAX_ROWS and lo < hi:
            conn.execute("UPDATE windows SET superseded = 1 WHERE id = ?", (window_id,))
            conn.commit()
            mid = lo + (hi - lo) // 2
            stack += [(mid + timedelta(days=1), hi), (lo, mid)]
            logging.info(f"{job} {lo}..{hi}: {rows} rows from an unpaged endpoint "
                         f"-- splitting the window in case it was cut off")
            continue
        total += rows
    return total


def _completed_bodies(conn, jobs: list[str], capture: str):
    """Bodies of every page of every complete, non-superseded window."""
    marks = ",".join("?" * len(jobs))
    return conn.execute(
        f"SELECT r.body FROM raw_responses r JOIN windows w ON w.id = r.window_id "  # noqa: S608
        f"WHERE w.job IN ({marks}) AND w.capture = ? AND w.completed_at IS NOT NULL "
        f"AND w.superseded = 0 ORDER BY r.id",
        [*jobs, capture],
    )


def captured_tickers(conn, jobs: list[str]) -> list[str]:
    """Distinct tickers in the completed history windows of `jobs`."""
    tickers = set()
    for body, in _completed_bodies(conn, jobs, "history"):
        for row in json.loads(body)["data"]:
            if isinstance(row, dict) and row.get("ticker"):
                tickers.add(row["ticker"])
    return sorted(tickers)


def pull(conn, client, job: str, end: date, tickers: Optional[list[str]] = None) -> None:
    """`tickers`, if given, replaces a per-ticker job's "tickers_from"."""
    spec = JOBS[job]
    if spec["mode"] == "once":
        if not _completed(conn, job, {}):
            _fetch_logged(conn, client, job, {})
    elif spec["mode"] == "range":
        pull_range(conn, client, job, HISTORY_START, end)
    elif spec["mode"] == "per_ticker":
        source = "--tickers-file" if tickers is not None else spec["tickers_from"]
        if tickers is None:
            tickers = captured_tickers(conn, spec["tickers_from"])
        logging.info(f"{job}: {len(tickers)} tickers from {source}")
        # one window per ticker: a ticker's whole history comes back in one
        # response (AAPL market cap: 2,784 rows), and these endpoints are
        # paged anyway, so yearly windows would only multiply the requests
        whole = (end - HISTORY_START).days + 1
        for i, ticker in enumerate(tickers, 1):
            if _ticker_completed(conn, job, ticker):
                continue
            try:
                pull_range(conn, client, job, HISTORY_START, end, extra={"ticker": ticker},
                           days=whole)
            except AlphanumeError as e:
                logging.warning(f"{job} {ticker}: {e}")
            if i % 100 == 0:
                logging.info(f"{job}: {i}/{len(tickers)} tickers done")
    else:
        raise ValueError(f"unknown mode {spec['mode']!r}")


def live(conn, client, job: str, today: date) -> int:
    """What the endpoint serves right now for the last few days, stored as
    capture='live', to compare later with the history for the same dates."""
    params = {"date_gte": (today - timedelta(days=LIVE_LOOKBACK_DAYS)).isoformat(),
              "date_lte": today.isoformat()}
    return _fetch_logged(conn, client, job, params, capture="live")[1]


def summary(conn) -> list[dict]:
    out = []
    for job in JOBS:
        for capture in ("history", "live"):
            n_pages = rows = 0
            dates, tickers = set(), set()
            for body, in _completed_bodies(conn, [job], capture):
                n_pages += 1
                for row in json.loads(body)["data"]:
                    rows += 1
                    if isinstance(row, dict):
                        if row.get("date"):
                            dates.add(str(row["date"]))
                        if row.get("ticker"):
                            tickers.add(row["ticker"])
            errors = conn.execute(
                "SELECT COUNT(*) FROM raw_responses r JOIN windows w ON w.id = r.window_id "
                "WHERE w.job = ? AND w.capture = ? AND r.http_status != 200",
                (job, capture),
            ).fetchone()[0]
            if n_pages or errors:
                out.append({"job": job, "capture": capture, "pages": n_pages, "rows": rows,
                            "first": min(dates, default=None), "last": max(dates, default=None),
                            "dates": len(dates), "tickers": len(tickers), "errors": errors})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", required=True, type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("jobs", nargs="*", help=f"any of: {', '.join(JOBS)}")
    p_pull.add_argument("--all", action="store_true")
    p_pull.add_argument("--tickers-file", type=Path,
                        help="one ticker per line: per-ticker jobs pull these instead of their tickers_from")
    p_live = sub.add_parser("live")
    p_live.add_argument("jobs", nargs="+", choices=list(JOBS))
    sub.add_parser("summary")
    args = parser.parse_args()

    conn = connect(args.db_path)
    if args.cmd == "summary":
        for row in summary(conn):
            print(row)
        return
    load_dotenv()
    client = Client(os.environ["ALPHANUME_API_KEY"])
    today = datetime.now(NY).date()
    if args.cmd == "pull":
        jobs = list(JOBS) if args.all else args.jobs
        tickers = None
        if args.tickers_file:
            tickers = [t.strip() for t in args.tickers_file.read_text().split() if t.strip()]
        if not jobs or set(jobs) - set(JOBS):
            parser.error(f"name jobs from {', '.join(JOBS)}, or use --all")
        for job in jobs:
            try:
                pull(conn, client, job, today, tickers)
            except AlphanumeError as e:
                logging.error(f"{job}: {e} -- stopping this job; rerun to resume")
    else:
        failed = False
        for job in args.jobs:
            try:
                live(conn, client, job, today)
            except AlphanumeError as e:
                logging.error(f"{job} live: {e}")
                failed = True
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
