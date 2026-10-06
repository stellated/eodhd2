"""Diff the `daily` table between two dated db snapshots (an anti-join on
code+timestamp) to see what price data a stretch of automated runs actually
added, and group the result by how many new rows each ticker picked up.

Findings from the 2026-09-30 vs 2026-10-05 (actually captured 2026-10-04,
right after that Saturday's daily-price run) comparison -- 2,996 new rows
across 665 tickers, falling into four distinct, fully-explained groups by
new-row count per ticker:

- 1-3 rows (607 tickers, the large majority): an already-open, ongoing tip
  whose window was caught up through 2026-09-29 -- these are just the
  handful of newly-published trading sessions (2026-09-30 through
  2026-10-02) landing on a window that was already otherwise complete. The
  exact count (1, 2, or 3) depends only on which of those days a given
  ticker's n2=20-day-after window had already reached.
- 7-12 rows (5 tickers): a brand-new tip landing on a ticker that already
  had data from an *older* tip, whose window mostly (but not entirely)
  overlaps the new tip's own n1=20-day-before window -- only the small
  non-overlapping tail needed fetching.
- 21-22 rows (49 tickers): a genuinely new ticker/tip with zero prior
  coverage -- the full n1=20-before + tip day (+ any n2-after days already
  elapsed) window fetched from scratch.
- 41-45 rows (4 tickers, e.g. GMGI.US, VSCO.US): a previously-delisted
  ticker (see doc/DESIGN_DECISIONS.md, "ticker_aliases...") whose *entire*
  tip window had zero coverage in the older snapshot -- once a
  ticker_aliases entry let a daily-price run actually resolve it, the full
  window was fetched for the first time in one shot.

In short: this confirms daily_update.py's backfill (_still_open_tips()),
the new-tip pickup, and the ticker_aliases delisting fix are all working
together correctly end-to-end -- every ticker's new-row count is explained
by exactly one of the above, no unaccounted-for gaps.
"""
from pathlib import Path
import polars as pl
from eodhd_io import Database

data_dir = Path('/Users/ianatkinson/Library/CloudStorage/OneDrive-Personal/Data/StockDataAnalytics/mars')
db1 = Database(data_dir / 'sda.2026-09-30.db')
df1 = db1.to_polars('daily')

db2 = Database(data_dir / 'sda.2026-10-05.db')
df2 = db2.to_polars('daily')

new_in_2 = df2.join(df1, on=['code', 'timestamp'], how='anti')
print(new_in_2)

d = {}
new_codes = sorted(list(set(new_in_2['code'])))
print(len(new_codes), 'new codes')
for i, code in enumerate(new_codes):
    print(code, len(new_in_2.filter(pl.col('code') == code)))
    length = len(new_in_2.filter(pl.col('code') == code))
    if length in d:
        d[length] += 1
    else:
        d[length] = 1
for length in sorted(d.keys()):
    print(length, d[length])

with (pl.Config(tbl_cols=-1, tbl_rows=-1)):
    print(
        new_in_2.filter(pl.col('code') == 'AAOI.US')
        .select('code', 'date')
        .with_columns(pl.col('date').dt.strftime("%A").alias('weekday'))
    )

