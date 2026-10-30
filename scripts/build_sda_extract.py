#!/usr/bin/env python3
"""Build sda's in-sample extract (its data request,
~/analysis/sda/proposals/eodhd2-sda.md) as one read-only SQLite file.

Sources, all opened read-only:
- sda.db (a pulled copy, never the one a run is writing): the tips,
  outcomes, daily and 5m bars, ticker_aliases, intraday_unavailable, and
  ig_margins for IG's later snapshots;
- alphanume.db: S&P 500 Risk Regime, Historical Market Cap, SEC Filing
  Intensity and Wikipedia Views (raw responses, parsed here);
- EODHD: SPY daily and 5m bars (6 calls).

In-sample is tips dated up to --last-tip-date (2026-09-01; tips from
09-02 are the hold-out, Ian's note `DAProjects/SDA Tips Database 2026.md`).
No tip, tip_exchange or outcomes row after it is copied. Price bars cover
each in-sample tip's own window, N1 sessions before to N2 after, as
tips() fetched them, so they reach into October.

Available dates (Ian, 2026-10-28: a field known only later carries the
date it became known; one whose date can't be identified is withheld):
- risk_regime: published at 10:10 ET on its `date`, so `available_at`
  is that time; a pre-open tip can only use an earlier date's label.
- market_cap, filing_intensity, wikipedia_views: Alphanume's publication
  time isn't documented. Its datasets appear in a nightly batch (Dilution's
  about 23:00 ET), so a row dated D is taken as available from the open
  ALPHANUME_LAG_SESSIONS sessions later, a margin of a day beyond that.
  Rows whose available_date is after the cut are left out.
- ig_margins: IG's files from 2025-10-06 and from 2026-10-22 (there is none
  from the in-sample period); `source_modified` is when IG generated each,
  so they describe IG's offering later than the tips.

Tables written: tip_exchange, tip_details, outcomes, daily, intraday_5m,
ticker_aliases, intraday_unavailable, tip_windows, risk_regime,
market_cap, filing_intensity, wikipedia_views, ig_margins, window_gaps,
outcome_gaps, extract_info. With --docs-out, also copies OUTCOMES_SCHEMA.md
and TICKER_ALIASES.md, the latter without hold-out tips (trim_aliases_doc).
The request's checks run at the end; a failed hard check raises before
the file is marked read-only.

  build_sda_extract.py --sda-db sda.db --alphanume-db ~/data/alphanume.db \\
      --out sda_insample.db --docs-out docs/
"""
import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_qgmi_extract import history_rows  # noqa: E402
from get_ig_margins import OUTPUT_COLUMNS as IG_COLUMNS  # noqa: E402
from get_ig_margins import create_resolved_view  # noqa: E402

from eodhd_io import (  # noqa: E402
    DEFAULT_N1,
    DEFAULT_N2,
    Database,
    _get_calendar,
    _n_sessions_before,
    add_local_time,
    fetch_daily,
    fetch_intraday,
    pandas2sqlite,
    unresolved_tips,
    update_outcomes,
)

LAST_TIP_DATE = date(2026, 9, 1)
SERIES_FROM = date(2025, 3, 1)  # Alphanume series start: a year before the first tip
ALPHANUME_LAG_SESSIONS = 2
REGIME_PUBLISHED = time(10, 10)  # ET, on the label's own date
IG_FILES = "source_modified = '2025-10-06' OR source_modified >= '2026-10-22'"
WINDOW_DAYS = DEFAULT_N2 * 2 + 5  # tips()'s calendar-day buffer after tip_date
NY = ZoneInfo("America/New_York")

# force: eodhd_io configures logging on import, which would make this a no-op
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    force=True)
log = logging.getLogger("build_sda_extract")


def window_end(dates: list[str], tip_date: str, n2: int) -> str:
    """Last date of a tip's window as tips() trims it: the n2-th date
    present after tip_date (tip_date itself, when present, is day 0), or
    the last one present if fewer. dates: sorted, within tips()'s buffer."""
    after = [d for d in dates if d >= tip_date]
    if not after:
        return tip_date
    return after[n2] if len(after) > n2 else after[-1]


def available_date(d: str, sessions: list[str], lag: int) -> str | None:
    """The session `lag` sessions after d (d need not be a session), or
    None if the calendar runs out. sessions: sorted ISO dates."""
    i = bisect_right(sessions, d) + lag - 1
    return sessions[i] if i < len(sessions) else None


def trim_aliases_doc(text: str, cut: str) -> str:
    """doc/TICKER_ALIASES.md without the hold-out: tip dates after `cut`
    are removed from each table's tip-date column, and a row left with none
    is dropped (its notes may describe hold-out tips and their outcomes)."""
    lines, col = [], None
    for line in text.splitlines(keepends=True):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and col is None:
            col = next((i for i, c in enumerate(cells)
                        if c.lower().startswith(("tip date", "tip_date"))), -1)
        elif line.startswith("|") and col >= 0 and not set(line.strip()) <= set("|-: "):
            kept = [d for d in cells[col].split(", ") if d <= cut]
            if not kept:
                continue
            cells[col] = ", ".join(kept)
            line = "| " + " | ".join(cells) + " |\n"
        elif not line.startswith("|"):
            col = None
        lines.append(line)
    return "".join(lines)


def reached_aliases(aliases: list[tuple], codes: set[str]) -> list[tuple]:
    """The ticker_aliases rows (old_code, new_code, ...) reachable from
    codes, following renames of renames."""
    reach, kept, grew = set(codes), [], True
    while grew:
        grew = False
        for row in aliases:
            if row[0] in reach and row not in kept:
                kept.append(row)
                if row[1] and row[1] not in reach:
                    reach.add(row[1])
                grew = True
    return kept


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True, cwd=Path(__file__).resolve().parent).stdout.strip()


def copy_ddl(out: sqlite3.Connection, name: str) -> None:
    out.execute(out.execute("SELECT sql FROM sda.sqlite_master WHERE name = ?",
                            (name,)).fetchone()[0].replace("IF NOT EXISTS ", ""))


def ticker_codes(codes: set[str]) -> dict[str, str]:
    """Alphanume ticker -> tip code, for the tipped codes (Alphanume's
    'BRK.B' is EODHD's 'BRK-B.US')."""
    return {code[:-3].replace("-", "."): code for code in codes if code.endswith(".US")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sda-db", required=True, type=Path)
    parser.add_argument("--alphanume-db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--docs-out", required=True, type=Path,
                        help="directory for the doc copies delivered with the extract")
    parser.add_argument("--last-tip-date", type=date.fromisoformat, default=LAST_TIP_DATE)
    args = parser.parse_args()
    cut = args.last_tip_date.isoformat()

    if args.out.exists():
        raise SystemExit(f"{args.out} exists; remove it first")
    out = sqlite3.connect(args.out)
    out.execute("ATTACH DATABASE ? AS sda", (f"file:{args.sda_db}?mode=ro",))

    # 1-2. Tips and outcomes, in-sample only
    for name in ("tip_exchange", "tip_details", "outcomes"):
        copy_ddl(out, name)
        out.execute(f"INSERT INTO main.{name} SELECT * FROM sda.{name} WHERE tip_date <= ?",
                    (cut,))
    out.commit()
    tips = out.execute("SELECT DISTINCT code, tip_date FROM tip_details "
                       "WHERE code IS NOT NULL ORDER BY tip_date, code").fetchall()
    codes = {c for c, _ in tips}
    log.info("%d tips (%d distinct code/date), %d codes, %d outcomes",
             out.execute("SELECT COUNT(*) FROM tip_details").fetchone()[0], len(tips),
             len(codes), out.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])

    # 3-4. Each tip's window, then the bars inside it. The end is found per
    # table, since a ticker's daily and 5m dates can differ.
    cal = _get_calendar("US")
    out.execute("CREATE TABLE tip_windows (code TEXT NOT NULL, tip_date TEXT NOT NULL, "
                "start_date TEXT NOT NULL, daily_end TEXT NOT NULL, intraday_end TEXT NOT NULL, "
                "PRIMARY KEY (code, tip_date))")
    by_code = defaultdict(list)
    for code, tip_date in tips:
        by_code[code].append(tip_date)
    for code, tip_dates in by_code.items():
        ends = {}
        for table, col in (("daily", "date"), ("intraday_5m", "local_date")):
            dates = [d for (d,) in out.execute(
                f"SELECT DISTINCT {col} FROM sda.{table} WHERE code = ? ORDER BY 1", (code,))]
            ends[table] = {
                t: window_end([d for d in dates if d <= (date.fromisoformat(t)
                               + timedelta(days=WINDOW_DAYS)).isoformat()], t, DEFAULT_N2)
                for t in tip_dates}
        for t in tip_dates:
            start = _n_sessions_before(cal, date.fromisoformat(t), DEFAULT_N1).isoformat()
            out.execute("INSERT INTO tip_windows VALUES (?, ?, ?, ?, ?)",
                        (code, t, start, ends["daily"][t], ends["intraday_5m"][t]))
    for table, col, end in (("daily", "date", "daily_end"),
                            ("intraday_5m", "local_date", "intraday_end")):
        copy_ddl(out, table)
        out.execute(f"INSERT OR IGNORE INTO main.{table} SELECT b.* FROM tip_windows w "
                    f"JOIN sda.{table} b ON b.code = w.code "
                    f"AND b.{col} BETWEEN w.start_date AND w.{end}")
        out.commit()
        log.info("%s: %d rows", table, out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # 5. Aliases (all) and EODHD's no-intraday confirmations (in-sample tips)
    copy_ddl(out, "ticker_aliases")
    copy_ddl(out, "intraday_unavailable")
    out.execute("INSERT INTO main.intraday_unavailable SELECT * FROM sda.intraday_unavailable "
                "WHERE tip_date <= ?", (cut,))
    out.commit()

    # 9. SPY, the market baseline, over the span of every window
    lo, hi = out.execute("SELECT MIN(start_date), MAX(MAX(daily_end, intraday_end)) "
                         "FROM tip_windows").fetchone()
    # Only aliases an in-sample tip reaches (following chains) by the last
    # window's end: any other is there for a hold-out tip, and would say so.
    aliases = out.execute("SELECT * FROM sda.ticker_aliases WHERE effective_date <= ?",
                          (hi,)).fetchall()
    out.executemany(f"INSERT INTO main.ticker_aliases VALUES ({', '.join('?' * len(aliases[0]))})",
                    reached_aliases(aliases, codes))
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ["EODHD_API_TOKEN"]
    pandas2sqlite(fetch_daily("SPY.US", token, date.fromisoformat(lo), date.fromisoformat(hi)),
                  out, "daily")
    from_ts = int(datetime.combine(date.fromisoformat(lo), time(0), NY).timestamp())
    to_ts = int(datetime.combine(date.fromisoformat(hi), time(23, 59), NY).timestamp())
    pandas2sqlite(add_local_time(fetch_intraday("SPY.US", token, "5m", from_ts, to_ts)),
                  out, "intraday_5m")
    out.execute("DELETE FROM intraday_5m WHERE code = 'SPY.US' AND local_date NOT BETWEEN ? AND ?",
                (lo, hi))
    out.commit()

    # 6-7. Alphanume, each row with the date it became known
    alpha = sqlite3.connect(f"file:{args.alphanume_db}?mode=ro", uri=True)
    sessions = [s.date().isoformat() for s in cal.sessions_in_range(
        SERIES_FROM.isoformat(), (args.last_tip_date + timedelta(days=30)).isoformat())]
    out.execute("CREATE TABLE risk_regime (date TEXT PRIMARY KEY, risk_regime INTEGER, "
                "available_at TEXT NOT NULL)")
    for r in history_rows(alpha, "risk_regime"):
        at = datetime.combine(date.fromisoformat(r["date"]), REGIME_PUBLISHED, NY)
        if SERIES_FROM.isoformat() <= r["date"] <= cut:
            out.execute("INSERT OR REPLACE INTO risk_regime VALUES (?, ?, ?)",
                        (r["date"], r["risk_regime"], at.isoformat()))

    code_of = ticker_codes(codes)
    datasets = {
        "market_cap": ["market_cap REAL", "shares_outstanding REAL"],
        "filing_intensity": ["filing_count INTEGER"],
        "wikipedia_views": ["views INTEGER", "avg_30d REAL", "zscore_30d REAL"],
    }
    for job, cols in datasets.items():
        names = [c.split()[0] for c in cols]
        out.execute(f"CREATE TABLE {job} (code TEXT NOT NULL, ticker TEXT NOT NULL, "
                    f"date TEXT NOT NULL, available_date TEXT NOT NULL, {', '.join(cols)}, "
                    f"PRIMARY KEY (code, date))")
        tickers = [t for (t,) in alpha.execute(
            "SELECT DISTINCT json_extract(params, '$.ticker') FROM windows WHERE job = ? "
            "AND capture = 'history' AND completed_at IS NOT NULL AND superseded = 0", (job,))
            if t in code_of]
        for t in tickers:
            rows = []
            for r in history_rows(alpha, job, t):
                if r["date"] < SERIES_FROM.isoformat():
                    continue
                avail = available_date(r["date"], sessions, ALPHANUME_LAG_SESSIONS)
                if avail is None or avail > cut:
                    continue
                rows.append((code_of[t], t, r["date"], avail, *(r.get(n) for n in names)))
            out.executemany(f"INSERT OR REPLACE INTO {job} VALUES "
                            f"({', '.join('?' * (4 + len(names)))})", rows)
        out.commit()
        log.info("%s: %d tickers, %d rows", job, len(tickers),
                 out.execute(f"SELECT COUNT(*) FROM {job}").fetchone()[0])
    alpha.close()

    # 8. IG, resolved through ticker_aliases, for the tipped codes. The view is
    # slow over all 3M rows: build it over just these snapshots.
    cols = [c for c in IG_COLUMNS if c != "code"]
    out.execute("CREATE TABLE ig_margins (download_date TEXT NOT NULL, code TEXT NOT NULL, "
                "ig_code TEXT, renamed_on TEXT, "
                + ", ".join(c for c in cols if c != "download_date")
                + ", PRIMARY KEY (download_date, code))")
    ig = sqlite3.connect(":memory:")
    ig.execute("ATTACH DATABASE ? AS sda", (f"file:{args.sda_db}?mode=ro",))
    for name in ("ig_margins", "ticker_aliases"):
        ig.execute(out.execute("SELECT sql FROM sda.sqlite_master WHERE name = ?",
                               (name,)).fetchone()[0].replace("IF NOT EXISTS ", ""))
    ig.execute("INSERT INTO main.ticker_aliases SELECT * FROM sda.ticker_aliases")
    ig.execute(f"INSERT INTO main.ig_margins SELECT * FROM sda.ig_margins WHERE {IG_FILES}")
    ig.commit()
    ig.execute("DETACH DATABASE sda")  # else the view's DROP VIEW finds sda's own
    create_resolved_view(ig)
    select = ["download_date", "code", "ig_code", "renamed_on"] + \
        [c for c in cols if c != "download_date"]
    ig.execute("CREATE TEMP TABLE tipped (code TEXT PRIMARY KEY)")
    ig.executemany("INSERT INTO tipped VALUES (?)", ((c,) for c in codes))
    out.executemany(f"INSERT INTO ig_margins VALUES ({', '.join('?' * len(select))})",
                    ig.execute(f"SELECT {', '.join('r.' + c for c in select)} "
                               "FROM ig_margins_resolved r JOIN tipped t ON t.code = r.code"))
    ig_files = [d for (d,) in ig.execute(
        "SELECT DISTINCT download_date FROM ig_margins ORDER BY 1")]
    ig.close()
    out.commit()
    out.execute("DETACH DATABASE sda")
    out.close()

    # Checks. update_outcomes() must change nothing: same tips, same bars.
    with Database(args.out) as db:
        before = db.conn.execute("SELECT * FROM outcomes ORDER BY code, tip_date").fetchall()
        update_outcomes(db)
        after = db.conn.execute("SELECT * FROM outcomes ORDER BY code, tip_date").fetchall()
        db.conn.execute("CREATE TABLE window_gaps (tablename TEXT NOT NULL, code TEXT NOT NULL, "
                        "tip_date TEXT NOT NULL, actual_days INTEGER, last_available_date TEXT, "
                        "fill_granularity TEXT, PRIMARY KEY (tablename, code, tip_date))")
        for table, interval in (("daily", "1d"), ("intraday_5m", "5m")):
            short = unresolved_tips(db, [(c, date.fromisoformat(t)) for c, t in tips],
                                    table, interval)
            for r in short.itertuples():
                fg = db.conn.execute("SELECT fill_granularity FROM outcomes "
                                     "WHERE code = ? AND tip_date = ?",
                                     (r.code, str(r.tip_date))).fetchone()
                db.conn.execute("INSERT INTO window_gaps VALUES (?, ?, ?, ?, ?, ?)",
                                (table, r.code, str(r.tip_date), r.actual_days,
                                 str(r.last_available_date), fg[0] if fg else None))
        db.conn.execute("CREATE TABLE outcome_gaps (code TEXT NOT NULL, tip_date TEXT NOT NULL, "
                        "reason TEXT NOT NULL, PRIMARY KEY (code, tip_date))")
        db.conn.execute("INSERT INTO outcome_gaps SELECT DISTINCT t.code, t.tip_date, "
                        "COALESCE(o.status, 'no_outcome') FROM tip_details t "
                        "LEFT JOIN outcomes o ON o.code = t.code AND o.tip_date = t.tip_date "
                        "WHERE t.code IS NOT NULL AND (o.code IS NULL OR o.status IN "
                        "('pending', 'open'))")
        db.conn.commit()
        conn = db.conn
        late = {name: conn.execute(f"SELECT MAX(tip_date) FROM {name}").fetchone()[0]
                for name in ("tip_exchange", "tip_details", "outcomes")}
        late.update({name: conn.execute(f"SELECT MAX(available_date) FROM {name}").fetchone()[0]
                     for name in datasets})
        if before != after:
            changed = len(set(before) ^ set(after))
            raise SystemExit(f"update_outcomes() changed the extract's outcomes ({changed} rows)")
        if any(v is not None and v > cut for v in late.values()):
            raise SystemExit(f"rows after the cut-off: {late}")
        n_gaps = conn.execute("SELECT tablename, COUNT(*) FROM window_gaps GROUP BY 1").fetchall()
        n_out_gaps = conn.execute("SELECT COUNT(*) FROM outcome_gaps").fetchone()[0]

    out = sqlite3.connect(args.out)
    out.execute("CREATE TABLE extract_info (key TEXT PRIMARY KEY, value TEXT)")
    info = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eodhd2_commit": git_commit(),
        "tips": f"{tips[0][1]}..{cut} ({len(tips)} code/date)",
        "window": f"{DEFAULT_N1} sessions before to {DEFAULT_N2} after each tip",
        "bars": f"{lo}..{hi}",
        "alphanume_series_from": SERIES_FROM.isoformat(),
        "alphanume_lag_sessions": str(ALPHANUME_LAG_SESSIONS),
        "regime_published_et": REGIME_PUBLISHED.isoformat(),
        "ig_snapshots": ", ".join(ig_files),
        "window_gaps": ", ".join(f"{t} {n}" for t, n in n_gaps) or "none",
        "outcome_gaps": str(n_out_gaps),
    }
    out.executemany("INSERT INTO extract_info VALUES (?, ?)", info.items())
    out.commit()
    out.execute("VACUUM")
    out.close()
    args.out.chmod(0o444)
    doc = Path(__file__).resolve().parent.parent / "doc"
    args.docs_out.mkdir(parents=True, exist_ok=True)
    (args.docs_out / "TICKER_ALIASES.md").write_text(
        trim_aliases_doc((doc / "TICKER_ALIASES.md").read_text(), cut))
    (args.docs_out / "OUTCOMES_SCHEMA.md").write_text((doc / "OUTCOMES_SCHEMA.md").read_text())
    log.info("wrote %s; %s", args.out, info)


if __name__ == "__main__":
    main()
