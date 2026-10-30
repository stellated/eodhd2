#!/usr/bin/env python3
"""Build qgmi's in-sample extract (its data request,
~/analysis/qgmi/proposals/eodhd2-qgmi.md) as one read-only SQLite file.

Sources, all opened read-only:
- alphanume.db: QGMI picks, Optionable Tickers snapshots, Historical
  Market Cap, Ticker Classification (raw responses, parsed here);
- universe.db: EODHD daily bars from get_universe_daily.py, with its
  fetch_status and ticker_aliases;
- sda.db: ig_margins, for IG's snapshot nearest before each rebalance.

Timing (Ian, 2026-10-29): QGMI publishes at 16:05 ET on `date` (the
rebalance date), traded at the next open, held to the open after the next
rebalance. So the in-sample extract holds Alphanume rows dated up to the
last in-sample rebalance (--last-rebalance) and daily bars up to the open
after the following one (--bars-through). The Optionable Tickers snapshot
dated on a rebalance date is that month's universe.

Tables written: qgmi_picks, optionable, daily, fetch_status,
ticker_aliases, market_cap, ticker_classification, sector_etf_map,
ig_margins, bar_gaps, universe_counts, extract_info. The checks the
request lists run at the end; a failed hard check raises, before the file
is marked read-only.

  build_qgmi_extract.py --alphanume-db ~/data/alphanume.db \\
      --universe-db ~/data/universe.db --sda-db sda.db --out qgmi_insample.db
"""
import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from get_ig_margins import OUTPUT_COLUMNS as IG_COLUMNS  # noqa: E402
from get_ig_margins import create_resolved_view  # noqa: E402
from get_universe_daily import BENCHMARKS, eodhd_code  # noqa: E402

from eodhd_io import _redact_token  # noqa: E402

FIRST_REBALANCE = date(2019, 1, 2)
LAST_REBALANCE = date(2024, 10, 1)
BARS_THROUGH = date(2024, 11, 4)
NOTIONAL_MIN = 50e6          # the article's $50m a day
PRICE_MISMATCH = (0.67, 1.5)  # EODHD close / (market_cap / shares), outside -> flagged

# Alphanume's sector (or industry, where it decides) -> SPDR sector ETF.
# Alphanume's sectors aren't GICS: e.g. GOOGL and META are GICS
# communication services (XLC) and AMZN consumer discretionary (XLY), but
# land wherever Alphanume put them. NULL sector -> no ETF.
SECTOR_ETF = {
    "communications_media": "XLC",
    "consumer_cyclical": "XLY",
    "energy_resources": "XLE",
    "essential_goods": "XLP",
    "finance": "XLF",
    "healthcare": "XLV",
    "industrial_transport": "XLI",
    "raw_materials": "XLB",
    "real_assets": "XLRE",
    "technology": "XLK",
    "utilities_infrastructure": "XLU",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_qgmi_extract")


def history_rows(alpha: sqlite3.Connection, job: str, ticker: str | None = None):
    """Every data row of a job's complete, non-superseded history windows."""
    sql = ("SELECT r.body FROM raw_responses r JOIN windows w ON w.id = r.window_id "
           "WHERE w.job = ? AND w.capture = 'history' AND w.completed_at IS NOT NULL "
           "AND w.superseded = 0")
    args: tuple = (job,)
    if ticker is not None:
        sql += " AND json_extract(w.params, '$.ticker') = ?"
        args += (ticker,)
    for (body,) in alpha.execute(sql, args):
        yield from json.loads(body)["data"]


def classify_bar(rebalance: str, bar: tuple | None, status: tuple | None) -> str | None:
    """Why a universe ticker has no usable bar on a rebalance date, or None.
    bar = (vo, cl) on that date; status = (status, first_date, last_date)
    from fetch_status (first/last are the first and last bars with volume)."""
    if status is None or status[0] != "ok" or status[1] is None:
        return "no_bars"
    if rebalance < status[1]:
        return "before_first_bar"
    if rebalance > status[2]:
        return "after_last_bar"
    if bar is None:
        return "missing_day"
    if bar[0] == 0:
        return "zero_volume"
    return None


def price_ratio(cl: float, market_cap: float | None, shares: float | None) -> float | None:
    """EODHD's close over Alphanume's implied price (market_cap / shares)."""
    if not market_cap or not shares or not cl:
        return None
    return cl / (market_cap / shares)


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True, cwd=Path(__file__).resolve().parent).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alphanume-db", required=True, type=Path)
    parser.add_argument("--universe-db", required=True, type=Path)
    parser.add_argument("--sda-db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--first-rebalance", type=date.fromisoformat, default=FIRST_REBALANCE)
    parser.add_argument("--last-rebalance", type=date.fromisoformat, default=LAST_REBALANCE)
    parser.add_argument("--bars-through", type=date.fromisoformat, default=BARS_THROUGH)
    args = parser.parse_args()
    first, last, through = (args.first_rebalance.isoformat(), args.last_rebalance.isoformat(),
                            args.bars_through.isoformat())

    if args.out.exists():
        raise SystemExit(f"{args.out} exists; remove it first")
    alpha = sqlite3.connect(f"file:{args.alphanume_db}?mode=ro", uri=True)
    out = sqlite3.connect(args.out)
    out.execute("ATTACH DATABASE ? AS uni", (f"file:{args.universe_db}?mode=ro",))

    # 1. QGMI picks
    out.execute("CREATE TABLE qgmi_picks (date TEXT NOT NULL, ticker TEXT NOT NULL, "
                "code TEXT NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY (date, rank))")
    out.executemany("INSERT INTO qgmi_picks VALUES (?, ?, ?, ?)", (
        (r["date"], r["ticker"], eodhd_code(r["ticker"]), int(r["rank"]))
        for r in history_rows(alpha, "qgmi") if first <= r["date"] <= last))

    # 2. Optionable Tickers snapshots
    out.execute("CREATE TABLE optionable (date TEXT NOT NULL, ticker TEXT NOT NULL, "
                "code TEXT NOT NULL, has_weeklies INTEGER, avg_days_between REAL, "
                "PRIMARY KEY (date, ticker))")
    out.executemany("INSERT INTO optionable VALUES (?, ?, ?, ?, ?)", (
        (r["date"], r["ticker"], eodhd_code(r["ticker"]), r["has_weeklies"],
         r["avg_days_between"])
        for r in history_rows(alpha, "optionable") if first <= r["date"] <= last))
    out.commit()
    rebalances = [d for (d,) in out.execute("SELECT DISTINCT date FROM qgmi_picks ORDER BY 1")]
    log.info("%d rebalances, %d picks, %d snapshot rows", len(rebalances),
             out.execute("SELECT COUNT(*) FROM qgmi_picks").fetchone()[0],
             out.execute("SELECT COUNT(*) FROM optionable").fetchone()[0])

    # Every code the extract covers: universe, picks, benchmarks.
    out.execute("CREATE TEMP TABLE codes (code TEXT PRIMARY KEY, ticker TEXT)")
    out.execute("INSERT OR IGNORE INTO codes SELECT code, ticker FROM optionable")
    out.execute("INSERT OR IGNORE INTO codes SELECT code, ticker FROM qgmi_picks")
    out.executemany("INSERT OR IGNORE INTO codes VALUES (?, ?)",
                    ((eodhd_code(t), t) for t in BENCHMARKS))

    # 3-4. Daily bars, fetch_status, ticker_aliases
    for name in ("daily", "fetch_status", "ticker_aliases"):
        out.execute(out.execute("SELECT sql FROM uni.sqlite_master WHERE name = ?",
                                (name,)).fetchone()[0])
    out.execute("INSERT INTO main.daily SELECT d.* FROM uni.daily d "
                "JOIN codes c ON c.code = d.code WHERE d.date <= ?", (through,))
    out.execute("INSERT INTO main.fetch_status SELECT f.* FROM uni.fetch_status f "
                "JOIN codes c ON c.code = f.code")
    out.execute("INSERT INTO main.ticker_aliases SELECT * FROM uni.ticker_aliases")
    # Errors recorded before eodhd_io redacted HTTPError messages carry the token.
    for code, err in out.execute("SELECT code, error FROM main.fetch_status "
                                 "WHERE error LIKE '%api_token=%'").fetchall():
        out.execute("UPDATE main.fetch_status SET error = ? WHERE code = ?",
                    (_redact_token(err), code))
    out.execute("CREATE INDEX daily_date ON daily (date, code)")
    out.commit()
    log.info("daily: %d rows", out.execute("SELECT COUNT(*) FROM daily").fetchone()[0])

    # 5. Market cap on each rebalance date (latest row on or before it), for
    # that month's universe and picks.
    months_of: dict[str, set[str]] = defaultdict(set)
    for d, t in out.execute("SELECT date, ticker FROM optionable UNION "
                            "SELECT date, ticker FROM qgmi_picks"):
        months_of[t].add(d)
    out.execute("CREATE TABLE market_cap (rebalance_date TEXT NOT NULL, ticker TEXT NOT NULL, "
                "code TEXT NOT NULL, date TEXT NOT NULL, market_cap REAL, "
                "shares_outstanding REAL, PRIMARY KEY (rebalance_date, ticker))")
    windows = alpha.execute(
        "SELECT json_extract(params, '$.ticker') FROM windows WHERE job = 'market_cap' "
        "AND capture = 'history' AND completed_at IS NOT NULL AND superseded = 0").fetchall()
    for (t,) in windows:
        if t not in months_of:
            continue
        rows = sorted((r["date"], r["market_cap"], r["shares_outstanding"])
                      for r in history_rows(alpha, "market_cap", t) if r["date"] <= last)
        i, best = 0, None
        for reb in sorted(months_of[t]):
            while i < len(rows) and rows[i][0] <= reb:
                best = rows[i]
                i += 1
            if best is not None:
                out.execute("INSERT INTO market_cap VALUES (?, ?, ?, ?, ?, ?)",
                            (reb, t, eodhd_code(t), *best))
    out.commit()

    # 6. Ticker Classification and the sector -> ETF map
    out.execute("CREATE TABLE sector_etf_map (alphanume_sector TEXT PRIMARY KEY, etf TEXT)")
    out.executemany("INSERT INTO sector_etf_map VALUES (?, ?)", SECTOR_ETF.items())
    out.execute("CREATE TABLE ticker_classification (ticker TEXT PRIMARY KEY, code TEXT, "
                "alphanume_sector TEXT, alphanume_industry TEXT, sector_etf TEXT)")
    out.executemany("INSERT INTO ticker_classification VALUES (?, ?, ?, ?, ?)", (
        (r["ticker"], eodhd_code(r["ticker"]), r["alphanume_sector"],
         r["alphanume_industry"], SECTOR_ETF.get(r["alphanume_sector"]))
        for r in history_rows(alpha, "ticker_classification") if r["ticker"] in months_of))
    out.commit()

    # 7. IG: for each rebalance, the latest snapshot IG generated on or before
    # it (it generates about 23:00 UTC, before the next open), resolved
    # through ticker_aliases, for that month's universe and picks.
    out.execute("ATTACH DATABASE ? AS sda", (f"file:{args.sda_db}?mode=ro",))
    snaps = out.execute("SELECT download_date, source_modified FROM sda.ig_margins "
                        "GROUP BY download_date ORDER BY source_modified, download_date"
                        ).fetchall()
    choice = {}
    for reb in rebalances:
        before = [s for s in snaps if s[1] <= reb]
        if before:
            choice[reb] = before[-1]
    cols = [c for c in IG_COLUMNS if c != "code"]
    out.execute("CREATE TABLE ig_margins (rebalance_date TEXT NOT NULL, code TEXT NOT NULL, "
                "ig_code TEXT, renamed_on TEXT, age_days INTEGER, "
                + ", ".join(cols) + ", PRIMARY KEY (rebalance_date, code))")
    # The view is slow over all 3M rows: build it over just the chosen snapshots.
    ig = sqlite3.connect(":memory:")
    ig.execute("ATTACH DATABASE ? AS sda", (f"file:{args.sda_db}?mode=ro",))
    ig.execute(out.execute("SELECT sql FROM sda.sqlite_master WHERE name = 'ig_margins'"
                           ).fetchone()[0].replace("IF NOT EXISTS ", ""))
    ig.execute(out.execute("SELECT sql FROM sda.sqlite_master WHERE name = 'ticker_aliases'"
                           ).fetchone()[0])
    ig.execute("INSERT INTO main.ticker_aliases SELECT * FROM sda.ticker_aliases")
    for dl in sorted({s[0] for s in choice.values()}):
        ig.execute("INSERT INTO main.ig_margins SELECT * FROM sda.ig_margins "
                   "WHERE download_date = ?", (dl,))
    ig.commit()
    ig.execute("DETACH DATABASE sda")  # else the view's DROP VIEW finds sda's own
    create_resolved_view(ig)
    ig.execute("CREATE TEMP TABLE resolved AS SELECT * FROM ig_margins_resolved")
    ig.execute("CREATE INDEX temp.resolved_ix ON resolved (download_date, code)")
    for reb, (dl, modified) in choice.items():
        codes = [c for (c,) in out.execute(
            "SELECT code FROM optionable WHERE date = ? UNION "
            "SELECT code FROM qgmi_picks WHERE date = ?", (reb, reb))]
        age = (date.fromisoformat(reb) - date.fromisoformat(modified)).days
        for code in codes:
            row = ig.execute("SELECT code, ig_code, renamed_on, " + ", ".join(cols)
                             + " FROM resolved WHERE download_date = ? AND code = ?",
                             (dl, code)).fetchone()
            if row:
                out.execute(f"INSERT INTO ig_margins VALUES ({', '.join('?' * (len(cols) + 5))})",
                            (reb, row[0], row[1], row[2], age, *row[3:]))
    ig.close()
    out.commit()
    out.execute("DETACH DATABASE sda")

    # 8. Bar gaps and the universe-size check, on each rebalance day's close.
    out.execute("CREATE TABLE bar_gaps (rebalance_date TEXT NOT NULL, code TEXT NOT NULL, "
                "ticker TEXT, in_picks INTEGER, reason TEXT NOT NULL, detail TEXT, "
                "PRIMARY KEY (rebalance_date, code))")
    out.execute("CREATE TABLE universe_counts (date TEXT PRIMARY KEY, snapshot INTEGER, "
                "has_weeklies INTEGER, weeklies_with_bar INTEGER, weeklies_50m INTEGER, "
                "all_50m INTEGER)")
    status = {c: (s, f, la) for c, s, f, la in out.execute(
        "SELECT code, status, first_date, last_date FROM fetch_status")}
    for reb in rebalances:
        members = out.execute(
            "SELECT o.code, o.ticker, o.has_weeklies, p.code IS NOT NULL FROM optionable o "
            "LEFT JOIN qgmi_picks p ON p.date = o.date AND p.code = o.code WHERE o.date = ? "
            "UNION SELECT p.code, p.ticker, NULL, 1 FROM qgmi_picks p WHERE p.date = ? "
            "AND p.code NOT IN (SELECT code FROM optionable WHERE date = ?)",
            (reb, reb, reb)).fetchall()
        n_week = n_week_bar = n_week_50 = n_all_50 = 0
        for code, ticker, weeklies, in_picks in members:
            bar = out.execute("SELECT vo, cl, hi, lo FROM daily WHERE code = ? AND date = ?",
                              (code, reb)).fetchone()
            reason = classify_bar(reb, bar[:2] if bar else None, status.get(code))
            detail = None
            if reason is None:
                mc = out.execute("SELECT date, market_cap, shares_outstanding FROM market_cap "
                                 "WHERE rebalance_date = ? AND code = ?", (reb, code)).fetchone()
                if mc and mc[0] == reb:
                    ratio = price_ratio(bar[1], mc[1], mc[2])
                    if ratio is not None and not PRICE_MISMATCH[0] <= ratio <= PRICE_MISMATCH[1]:
                        reason, detail = "price_mismatch", f"close/implied {ratio:.3f}"
            elif status.get(code):
                detail = f"bars {status[code][1]}..{status[code][2]}"
            if reason:
                out.execute("INSERT INTO bar_gaps VALUES (?, ?, ?, ?, ?, ?)",
                            (reb, code, ticker, in_picks, reason, detail))
            notional_ok = bar is not None and bar[0] > 0 and \
                (bar[2] + bar[3] + bar[1]) / 3 * bar[0] >= NOTIONAL_MIN
            n_all_50 += notional_ok and weeklies is not None
            if weeklies == 1:
                n_week += 1
                n_week_bar += bar is not None and bar[0] > 0
                n_week_50 += notional_ok
        n_snap = out.execute("SELECT COUNT(*) FROM optionable WHERE date = ?", (reb,)).fetchone()[0]
        out.execute("INSERT INTO universe_counts VALUES (?, ?, ?, ?, ?, ?)",
                    (reb, n_snap, n_week, n_week_bar, n_week_50, n_all_50))
    out.commit()

    # Checks
    bad_months = out.execute("SELECT date, COUNT(*), COUNT(DISTINCT rank) FROM qgmi_picks "
                             "GROUP BY date HAVING COUNT(*) != 10 OR COUNT(DISTINCT rank) != 10"
                             ).fetchall()
    late = {
        "daily": out.execute("SELECT MAX(date) FROM daily").fetchone()[0],
        "qgmi_picks": out.execute("SELECT MAX(date) FROM qgmi_picks").fetchone()[0],
        "optionable": out.execute("SELECT MAX(date) FROM optionable").fetchone()[0],
        "market_cap": out.execute("SELECT MAX(date) FROM market_cap").fetchone()[0],
    }
    if bad_months:
        raise SystemExit(f"months without exactly 10 picks: {bad_months}")
    if late["daily"] > through or any(v > last for k, v in late.items() if k != "daily"):
        raise SystemExit(f"rows after the cut-off: {late}")
    missing_snap = [d for d in rebalances if not out.execute(
        "SELECT 1 FROM optionable WHERE date = ? LIMIT 1", (d,)).fetchone()]
    if missing_snap:
        raise SystemExit(f"rebalances without a snapshot: {missing_snap}")

    out.execute("CREATE TABLE extract_info (key TEXT PRIMARY KEY, value TEXT)")
    info = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eodhd2_commit": git_commit(),
        "rebalances": f"{first}..{last} ({len(rebalances)})",
        "bars_through": through,
        "alphanume_rows_through": last,
        "ig_snapshots": ", ".join(sorted({s[0] for s in choice.values()})),
        "notional_min": str(NOTIONAL_MIN),
        "price_mismatch_band": f"{PRICE_MISMATCH[0]}..{PRICE_MISMATCH[1]}",
    }
    out.executemany("INSERT INTO extract_info VALUES (?, ?)", info.items())
    out.commit()
    out.execute("DETACH DATABASE uni")
    out.execute("VACUUM")
    out.close()
    args.out.chmod(0o444)
    log.info("wrote %s; %s", args.out, info)


if __name__ == "__main__":
    main()
