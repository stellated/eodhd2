#!/usr/bin/env python3
"""Turn an alphanume pull (alphanume.db, from get_alphanume.py) into an
EODHD fetch list and a call/storage estimate. Makes no EODHD price calls.

For each strategy dataset, every row is an event (ticker, date); NEEDS says
which EODHD data each event needs (see the pull list in
DAProjects/Alphanume Trial Plan.md). The plan is made per job and for all
jobs together (a ticker needed by two jobs is fetched once).

Requests and their cost (checked against the account's usage counter,
2026-10-23):
  daily     one /eod request per ticker covers any date range: 1 call.
            Stored rows are only the sessions inside the needed windows.
  intraday  5 calls per request; one request spans at most 600 days (5m)
            or 120 days (1m). Two counts are given: one request per event
            day (what tips() does today) and the event days merged into as
            few spans as the limits allow (needs a new fetch path).
            5m starts 2020-10; earlier NDM days use 1m aggregated to 5m.
  splits    one /splits request per Reverse Splits ticker: 1 call.

The daily limit is the account's dailyRateLimit (100,000 on 2026-10-23),
shared with the nightly tips jobs.

Things the output flags rather than solves:
  - daily sessions before 2006-06-30, which csv2pandas_daily() clips;
  - 1m days before 2015, where EODHD 1m coverage hasn't been checked;
  - tickers missing from EODHD's symbol lists (--symbols), which need
    alias research before they can be fetched;
  - rows without a usable date (the date field names are a guess until
    the first real responses are seen: DATE_FIELDS).

Usage:
  eodhd_budget.py --db-path ~/data/alphanume.db [--symbols US.csv US_delisted.csv]
                  [--fetch-list fetch_list.csv] [--calls-per-day 50000]
  eodhd_budget.py fetch-symbols DIR    download the two symbol lists (2 calls)
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd
import requests
from dotenv import load_dotenv

try:
    from scripts import get_alphanume as ga
except ImportError:  # run as scripts/eodhd_budget.py
    import get_alphanume as ga

CALLS = {"daily": 1, "intraday": 5, "splits": 1}
MAX_SPAN_DAYS = {"5m": 600, "1m": 120}
FIVE_MIN_START = date(2020, 10, 1)
ONE_MIN_CHECKED_FROM = date(2015, 1, 1)
DAILY_CLIP_BEFORE = date(2006, 6, 30)  # XNYS first_session in eodhd_io
# Measured on production sda.db, 2026-10-23: table + primary-key index bytes
# over row count; 5m holds 79 bars per ticker-day (09:30-16:00).
BYTES_PER_ROW = {"daily": 151, "intraday": 126}
BARS_PER_DAY = {"5m": 79, "1m": 960}  # 1m includes 04:00-20:00, padded

# Which row field holds the event date, in order of preference.
DATE_FIELDS = {
    "ndm": ["date"],
    "premarket_drop": ["date"],
    "qgmi": ["date", "rebalance_date", "month"],
    "reverse_splits": ["effective_date", "ex_date", "date"],
    "dilution": ["filing_date", "date"],
}
# daily: calendar days before/after the anchor day. anchor "next_session":
# NDM's date is the pick date and the trade is the next session (to check
# against the first real response). intraday: interval for the anchor day.
NEEDS = {
    "ndm": {"anchor": "next_session", "daily": (30, 0), "intraday": "5m"},
    "premarket_drop": {"anchor": "same", "daily": (30, 0), "intraday": "1m"},
    "qgmi": {"anchor": "same", "daily": (365, 31)},
    "reverse_splits": {"anchor": "same", "daily": (30, 30), "splits": True},
    "dilution": {"anchor": "same", "daily": (365, 30)},
}


def calendar():
    return xcals.get_calendar("XNYS", start="1995-01-01")


def eodhd_code(ticker: str) -> str:
    """alphanume 'BRK.B' -> EODHD 'BRK-B.US'."""
    return f"{ticker.strip().upper().replace('.', '-')}.US"


def _parse_date(value) -> date | None:
    s = str(value or "").strip()
    try:
        return date.fromisoformat(s[:10] if len(s) >= 10 else f"{s[:7]}-01")
    except ValueError:
        return None


def _next_session(cal, d: date) -> date:
    ts = pd.Timestamp(d)
    if cal.is_session(ts):
        return cal.next_session(ts).date()
    return cal.date_to_session(ts, direction="next").date()


def events(conn, job: str, cal) -> tuple[list[tuple[str, date]], int]:
    """(EODHD code, anchor day) for every history row of `job`, deduplicated,
    plus the number of rows skipped for having no ticker or usable date."""
    out, skipped = set(), 0
    for body, in ga._completed_bodies(conn, [job], "history"):
        for row in json.loads(body)["data"]:
            ticker = row.get("ticker") if isinstance(row, dict) else None
            day = None
            if ticker:
                day = next((d for f in DATE_FIELDS[job] if (d := _parse_date(row.get(f)))), None)
            if day is None:
                skipped += 1
                continue
            if NEEDS[job]["anchor"] == "next_session":
                day = _next_session(cal, day)
            out.add((eodhd_code(ticker), day))
    return sorted(out), skipped


def merge_intervals(intervals) -> list[tuple[date, date]]:
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def spans(days, max_days: int) -> list[tuple[date, date]]:
    """Fewest (first, last) spans of at most max_days calendar days covering
    every day in `days`."""
    out = []
    for d in sorted(set(days)):
        if out and (d - out[-1][0]).days < max_days:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def _intraday_interval(job: str, day: date) -> str:
    want = NEEDS[job]["intraday"]
    return "1m" if want == "5m" and day < FIVE_MIN_START else want


def plan(job_events: dict[str, list[tuple[str, date]]], cal) -> dict:
    """Requests, calls and stored rows for the events of one or more jobs."""
    daily = defaultdict(list)           # code -> [(from, to)]
    intraday = defaultdict(set)         # (code, interval) -> {day}
    stored_bars = defaultdict(int)      # (code, interval) -> bars per day stored
    splits = set()
    for job, evs in job_events.items():
        need = NEEDS[job]
        for code, day in evs:
            before, after = need["daily"]
            daily[code].append((day - timedelta(days=before), day + timedelta(days=after)))
            if "intraday" in need:
                iv = _intraday_interval(job, day)
                intraday[(code, iv)].add(day)
                # NDM's pre-2020 1m days are aggregated to 5m before storing
                bars = BARS_PER_DAY["5m"] if need["intraday"] == "5m" else BARS_PER_DAY[iv]
                stored_bars[(code, iv)] = max(stored_bars[(code, iv)], bars)
            if need.get("splits"):
                splits.add(code)

    fetch_list, daily_rows, clipped = [], 0, 0
    for code, windows in sorted(daily.items()):
        merged = merge_intervals(windows)
        for lo, hi in merged:
            n = len(cal.sessions_in_range(pd.Timestamp(lo), pd.Timestamp(hi)))
            daily_rows += n
            if lo < DAILY_CLIP_BEFORE:
                clipped += len(cal.sessions_in_range(
                    pd.Timestamp(lo), pd.Timestamp(min(hi, DAILY_CLIP_BEFORE - timedelta(days=1)))))
        fetch_list.append({"code": code, "kind": "daily", "from": merged[0][0],
                           "to": merged[-1][1], "days": sum((h - lo).days + 1 for lo, h in merged)})

    per_event = merged_reqs = intraday_rows = unchecked_1m = 0
    for (code, iv), days in sorted(intraday.items()):
        per_event += len(days)
        intraday_rows += len(days) * stored_bars[(code, iv)]
        unchecked_1m += sum(1 for d in days if iv == "1m" and d < ONE_MIN_CHECKED_FROM)
        for lo, hi in spans(days, MAX_SPAN_DAYS[iv]):
            merged_reqs += 1
            fetch_list.append({"code": code, "kind": iv, "from": lo, "to": hi,
                               "days": sum(1 for d in days if lo <= d <= hi)})
    for code in sorted(splits):
        fetch_list.append({"code": code, "kind": "splits", "from": None, "to": None, "days": None})

    base = len(daily) * CALLS["daily"] + len(splits) * CALLS["splits"]
    return {
        "tickers": len({c for c in daily}),
        "events": sum(len(e) for e in job_events.values()),
        "daily_requests": len(daily),
        "intraday_requests_per_event": per_event,
        "intraday_requests_merged": merged_reqs,
        "splits_requests": len(splits),
        "calls_per_event": base + per_event * CALLS["intraday"],
        "calls_merged": base + merged_reqs * CALLS["intraday"],
        "daily_rows": daily_rows,
        "intraday_rows": intraday_rows,
        "mb": (daily_rows * BYTES_PER_ROW["daily"]
               + intraday_rows * BYTES_PER_ROW["intraday"]) / 1e6,
        "daily_sessions_clipped": clipped,
        "unchecked_1m_days": unchecked_1m,
        "fetch_list": fetch_list,
    }


def read_symbols(paths) -> set[str]:
    """EODHD codes ('AAPL.US') from exchange-symbol-list CSVs."""
    codes = set()
    for path in paths:
        with open(path, newline="") as f:
            codes |= {f"{r['Code'].upper()}.US" for r in csv.DictReader(f) if r.get("Code")}
    return codes


def fetch_symbols(out_dir: Path) -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, extra in (("US.csv", {}), ("US_delisted.csv", {"delisted": 1})):
        resp = requests.get("https://eodhd.com/api/exchange-symbol-list/US", timeout=120,
                            params={"api_token": os.environ["EODHD_API_TOKEN"], "fmt": "csv",
                                    **extra})
        resp.raise_for_status()
        (out_dir / name).write_text(resp.text)
        print(f"{out_dir / name}: {resp.text.count(chr(10)) - 1} symbols")


def report(conn, symbols: set[str] | None):
    cal = calendar()
    job_events, rows = {}, []
    for job in NEEDS:
        evs, skipped = events(conn, job, cal)
        if not evs and not skipped:
            continue
        job_events[job] = evs
        p = plan({job: evs}, cal)
        unknown = sorted({c for c, _ in evs} - symbols) if symbols is not None else None
        rows.append((job, p, skipped, unknown, evs))
    return rows, plan(job_events, cal)


def _print(rows, total, calls_per_day):
    cols = ["events", "tickers", "daily_requests", "intraday_requests_per_event",
            "intraday_requests_merged", "splits_requests", "calls_per_event", "calls_merged",
            "daily_rows", "intraday_rows"]
    for job, p, skipped, unknown, evs in rows:
        print(f"\n{job}: " + ", ".join(f"{c}={p[c]:,}" for c in cols) + f", mb={p['mb']:,.0f}")
        if evs:
            print(f"  dates {min(d for _, d in evs)}..{max(d for _, d in evs)}")
        if skipped:
            print(f"  WARNING {skipped:,} rows skipped: no ticker or no date in {DATE_FIELDS[job]}")
        if p["daily_sessions_clipped"]:
            print(f"  WARNING {p['daily_sessions_clipped']:,} daily sessions before "
                  f"{DAILY_CLIP_BEFORE}, which csv2pandas_daily() clips")
        if p["unchecked_1m_days"]:
            print(f"  WARNING {p['unchecked_1m_days']:,} 1m days before {ONE_MIN_CHECKED_FROM} "
                  f"(EODHD 1m coverage not checked)")
        if unknown is not None:
            n_ev = sum(1 for c, _ in evs if c in set(unknown))
            print(f"  {len(unknown):,} tickers ({n_ev:,} events) not in EODHD's symbol lists"
                  + (f", e.g. {', '.join(unknown[:10])}" if unknown else ""))
    print("\nall jobs, each ticker fetched once: "
          + ", ".join(f"{c}={total[c]:,}" for c in cols) + f", mb={total['mb']:,.0f}")
    for label, key in (("one request per event day", "calls_per_event"),
                       ("merged spans", "calls_merged")):
        print(f"  {label}: {total[key]:,} calls = {total[key] / calls_per_day:.1f} days "
              f"at {calls_per_day:,} a day")


def main() -> None:
    if sys.argv[1:2] == ["fetch-symbols"]:
        fetch_symbols(Path(sys.argv[2]))
        return
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--symbols", nargs="+", type=Path,
                        help="EODHD exchange-symbol-list CSVs (see fetch-symbols)")
    parser.add_argument("--fetch-list", type=Path, help="write the merged fetch list here")
    parser.add_argument("--calls-per-day", type=int, default=50_000)
    args = parser.parse_args()

    conn = ga.connect(args.db_path)
    symbols = read_symbols(args.symbols) if args.symbols else None
    rows, total = report(conn, symbols)
    _print(rows, total, args.calls_per_day)
    if args.fetch_list:
        with open(args.fetch_list, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["code", "kind", "from", "to", "days"])
            w.writeheader()
            w.writerows(total["fetch_list"])
        print(f"\n{len(total['fetch_list']):,} requests written to {args.fetch_list}")


if __name__ == "__main__":
    main()
