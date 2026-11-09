#!/usr/bin/env python3
"""Build jml's in-sample extract (its data request,
~/analysis/jml/proposals/eodhd2-jml.md) as one read-only SQLite file.

Source: jml.db (scripts/jml_screen.py: the screen, the draw and the drawn
stocks' daily bars), opened read-only. No API calls.

In-sample is the draw's in-sample stocks. The hold-out is by stock, not
by date (Ian, 2026-11-05): `stock_pool` marks a quarter of all eligible
stocks as hold-out, and none of them, drawn or not, appears anywhere in
this extract. Not their bars, not their draw rows, not their eligible
rows, and not a screen_dropped row whose detail names one (a rename
duplicate's kept code). Only the pool's per-band counts are given, so
inclusion probabilities can be checked.

Each in-sample stock's bars cover its stock-period span(s): 5 years before
its period to a year after (jml_screen.stock_period_span). SPY covers every
span.

Tables written: draw, draw_rejects (in-sample stock-periods replaced by
jml_screen.py validate, and why), daily, fetch_status, eligible, pool_counts,
screen_dropped, series_gaps, periods, extract_info. The checks run at the
end; a failure raises before the file is marked read-only.

  build_jml_extract.py --jml-db ~/data/jml.db --out jml_insample.db
"""
import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jml_screen import (  # noqa: E402
    DRAW_SIZES,
    FETCH_MARGIN_DAYS,
    HOLD_OUT_SHARE,
    MIN_PRICE,
    MIN_TURNOVER,
    PERIODS,
    SCREEN_SESSIONS,
    SEED,
    stock_period_span,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    force=True)
log = logging.getLogger("build_jml_extract")

# a series is short if its last bar is this far before the span's end (the
# span already has FETCH_MARGIN_DAYS of margin; a week more covers holidays)
SHORT_DAYS = FETCH_MARGIN_DAYS + 7


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True, cwd=Path(__file__).resolve().parent).stdout.strip()


def build(src_path: Path, out_path: Path) -> None:
    if out_path.exists():
        raise SystemExit(f"{out_path} exists")
    out = sqlite3.connect(out_path)
    out.execute("ATTACH DATABASE ? AS j", (f"file:{src_path}?mode=ro",))

    out.execute("CREATE TABLE periods (period INTEGER PRIMARY KEY, start_date TEXT, "
                "end_date TEXT, span_from TEXT, span_to TEXT)")
    out.executemany("INSERT INTO periods VALUES (?, ?, ?, ?, ?)", [
        (p, s.isoformat(), e.isoformat(), *(d.isoformat() for d in stock_period_span(s, e)))
        for p, s, e in PERIODS])

    out.execute("CREATE TEMP TABLE hold AS SELECT code FROM j.stock_pool WHERE sample = 'hold_out'")
    out.execute("CREATE UNIQUE INDEX temp.hold_code ON hold (code)")

    out.execute("CREATE TABLE draw AS SELECT period, code, band, draw_rank, inclusion_prob, "
                "eligible_in_pool, seed, drawn_at FROM j.draw WHERE sample = 'in_sample'")
    out.execute("CREATE TABLE draw_rejects AS SELECT period, code, band, draw_rank, reason, "
                "replaced_by FROM j.draw_rejects WHERE sample = 'in_sample'")
    out.execute("CREATE TABLE fetch_status AS SELECT f.* FROM j.fetch_status f "
                "JOIN draw USING (period, code)")
    out.execute("INSERT INTO fetch_status SELECT * FROM j.fetch_status "
                "WHERE period = 0 AND code = 'SPY.US'")

    # bars: each in-sample stock within its own span(s), SPY over all of them
    ddl = out.execute("SELECT sql FROM j.sqlite_master WHERE name = 'daily'").fetchone()[0]
    out.execute(ddl.replace("IF NOT EXISTS ", ""))
    out.execute("""INSERT OR IGNORE INTO daily SELECT b.* FROM j.daily b
        JOIN draw d ON d.code = b.code JOIN periods p ON p.period = d.period
        WHERE b.date BETWEEN p.span_from AND p.span_to""")
    out.execute("""INSERT OR IGNORE INTO daily SELECT * FROM j.daily WHERE code = 'SPY.US'
        AND date BETWEEN (SELECT MIN(span_from) FROM periods)
                     AND (SELECT MAX(span_to) FROM periods)""")
    log.info("daily: %d rows", out.execute("SELECT COUNT(*) FROM daily").fetchone()[0])

    # the eligible list without the hold-out pool; the pool only as counts
    out.execute("CREATE TABLE eligible AS SELECT * FROM j.eligible "
                "WHERE code NOT IN (SELECT code FROM temp.hold)")
    out.execute("""CREATE TABLE pool_counts AS SELECT e.period, e.band,
        SUM(e.code NOT IN (SELECT code FROM temp.hold)) AS in_sample_pool,
        SUM(e.code IN (SELECT code FROM temp.hold)) AS hold_out_pool
        FROM j.eligible e GROUP BY 1, 2""")
    out.execute("""CREATE TABLE screen_dropped AS SELECT * FROM j.screen_dropped
        WHERE code NOT IN (SELECT code FROM temp.hold)
        AND COALESCE(detail, '') || '.US' NOT IN (SELECT code FROM temp.hold)""")

    # series that start late or stop before the span ends: listed, not dropped
    out.execute("""CREATE TABLE series_gaps AS SELECT f.period, f.code, f.status, f.first_date,
        f.last_date, p.span_from, p.span_to, p.end_date AS period_end,
        CASE WHEN f.status = 'error' THEN 'no_data'
             WHEN f.last_date < p.end_date THEN 'stops_in_period'
             WHEN f.last_date < date(p.span_to, ?) THEN 'stops_in_outcome_year'
             ELSE 'starts_late' END AS kind, f.error
        FROM fetch_status f JOIN periods p USING (period)
        WHERE f.status = 'error' OR f.last_date < date(p.span_to, ?)
           OR f.first_date > date(p.span_from, ?)""",
                (f"-{SHORT_DAYS} days", f"-{SHORT_DAYS} days", f"+{SHORT_DAYS} days"))

    info = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eodhd2_commit": git_commit(), "source": str(src_path),
        "seed": str(SEED), "hold_out_share_of_stocks": str(HOLD_OUT_SHARE),
        "draw_sizes_in_sample": ", ".join(f"{b} {n}" for b, (n, _) in DRAW_SIZES.items()),
        "screen": f"{SCREEN_SESSIONS}-session median close x volume >= {MIN_TURNOVER:,.0f}; "
                  f"close >= {MIN_PRICE}; traded 5 years before the period start",
        "drawn_at": out.execute("SELECT MAX(drawn_at) FROM draw").fetchone()[0],
    }
    out.execute("CREATE TABLE extract_info (key TEXT PRIMARY KEY, value TEXT)")
    out.executemany("INSERT INTO extract_info VALUES (?, ?)", info.items())
    out.commit()

    check(out)
    out.execute("DETACH DATABASE j")
    out.execute("VACUUM")
    out.close()
    os.chmod(out_path, 0o444)
    log.info("built %s (%.0f MB)", out_path, out_path.stat().st_size / 1e6)


def check(out: sqlite3.Connection) -> None:
    """Hard checks; any failure stops the build."""
    for table, col in [("draw", "code"), ("draw_rejects", "code"),
                       ("draw_rejects", "replaced_by"), ("daily", "code"), ("fetch_status", "code"),
                       ("eligible", "code"), ("screen_dropped", "code"),
                       ("series_gaps", "code")]:
        n = out.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN "
                        f"(SELECT code FROM temp.hold)").fetchone()[0]
        if n:
            raise SystemExit(f"hold-out stock in {table}: {n} rows")
    n = out.execute("SELECT COUNT(*) FROM screen_dropped WHERE detail || '.US' IN "
                    "(SELECT code FROM temp.hold)").fetchone()[0]
    if n:
        raise SystemExit(f"screen_dropped names a hold-out stock: {n} rows")
    for period, band, n_in in out.execute(
            "SELECT period, band, COUNT(*) FROM draw GROUP BY 1, 2").fetchall():
        if n_in != DRAW_SIZES[band][0]:
            raise SystemExit(f"period {period} {band}: {n_in} in-sample stocks")
    n = out.execute("SELECT COUNT(*) FROM draw WHERE (period, code) NOT IN "
                    "(SELECT period, code FROM fetch_status)").fetchone()[0]
    if n:
        raise SystemExit(f"{n} drawn stock-periods were never fetched")
    n = out.execute("""SELECT COUNT(*) FROM daily b WHERE code != 'SPY.US' AND NOT EXISTS (
        SELECT 1 FROM draw d JOIN periods p USING (period)
        WHERE d.code = b.code AND b.date BETWEEN p.span_from AND p.span_to)""").fetchone()[0]
    if n:
        raise SystemExit(f"{n} daily rows outside their stock's spans")
    for kind, n in out.execute("SELECT kind, COUNT(*) FROM series_gaps GROUP BY 1"):
        log.info("series_gaps %s: %d", kind, n)
    log.info("checks passed")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jml-db", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    build(args.jml_db, args.out)


if __name__ == "__main__":
    main()
