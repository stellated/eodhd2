# Technical Specification — EODHD Data Pipeline

This document is a prompt for a new LLM instance to support continued
development of this codebase. It should be read alongside the actual source
files (eodhd_io.py, tips_io.py, test_eodhd.py).

---

## Project overview

A personal Python library for ingesting stock market data from the EODHD API
into a local SQLite database, with conversion helpers for pandas and polars
DataFrames, and a separate module for parsing stock tip emails from a tipping
newsletter into the same database.

The human (Ian, developer, Melbourne Australia) uses this for backtesting tips
from a stock tipping newsletter against actual EODHD price data.

---

## File structure

```
eodhd_io.py   — core data pipeline: CSV parsing, format conversion, SQLite
                persistence, EODHD network fetch, Database class
tips_io.py    — email parsing: StockDataAnalytics .eml → pandas → SQLite
test_eodhd.py — pytest suite covering both modules
```

The split between eodhd_io.py and tips_io.py is deliberate. tips_io.py has
no import dependency on eodhd_io.py. The tips() function in eodhd_io.py
imports Database from eodhd_io and is the only coupling point.

---

## eodhd_io.py — full specification

### Column contracts

**Daily DataFrame:**
| Column    | pandas dtype     | polars dtype     | SQLite   |
|-----------|-----------------|-----------------|---------|
| code      | StringDtype      | pl.Utf8         | TEXT    |
| timestamp | int64            | pl.Int64        | INTEGER |
| datetime  | datetime64[us]   | pl.Datetime(us) | TEXT "YYYY-MM-DD HH:MM:SS" |
| date      | object (date)    | pl.Date         | TEXT "YYYY-MM-DD" |
| op hi lo cl ac | float64   | pl.Float64      | REAL    |
| vo        | int64            | pl.Int64        | INTEGER |

**Intraday DataFrame:**
| Column     | pandas dtype    | polars dtype     | SQLite  |
|------------|----------------|-----------------|---------|
| code       | StringDtype     | pl.Utf8         | TEXT    |
| timestamp  | int64           | pl.Int64        | INTEGER |
| datetime   | datetime64[us]  | pl.Datetime(us) | TEXT    |
| local_date | object (date)   | pl.Date         | TEXT "YYYY-MM-DD" |
| local_time | str (optional)  | pl.Utf8         | TEXT "HH:MM:SS" (optional col) |
| op hi lo cl | float64        | pl.Float64      | REAL    |
| vo         | int64           | pl.Int64        | INTEGER |

Note: intraday has no `ac` (adjusted close) column.
Note: `local_time` is added by `add_local_time()`, not by `csv2pandas_intraday()`.
Note: `datetime` is always UTC tz-naive. `local_date` and `local_time` are in
      exchange local timezone, derived from `timestamp` + the timezone in
      EXCHANGE_INFO for that exchange suffix.

### Primary keys
Both daily and intraday tables use `PRIMARY KEY (code, timestamp)`.
All writes use `INSERT OR REPLACE` — re-importing is idempotent.

### EXCHANGE_INFO dictionary
Module-level dict keyed by EODHD exchange suffix (without dot):
```python
EXCHANGE_INFO = {
    "LSE": {"calendar": "XLON", "tz": "Europe/London",
            "open": time(8,0), "close": time(16,30)},
    "US":  {"calendar": "XNYS", "tz": "America/New_York",
            "open": time(9,30), "close": time(16,0)},
    "AU":  {"calendar": "XASX", "tz": "Australia/Sydney",
            "open": time(10,0), "close": time(16,0)},
}
```
To add a new exchange: append one entry here. Everything else picks it up.

### Default policy dictionaries (module-level)
```python
DEFAULT_N_DAYS = {"1d": 60, "1m": 5, "5m": 10, "1h": 20}
DEFAULT_N1 = 20  # trading days before tip date for tips()
DEFAULT_N2 = 20  # trading days after tip date for tips()
```

### Public functions

**csv2pandas_daily(code: str, csv_path: Path) -> pd.DataFrame**
- Reads EODHD daily CSV (ISO date format YYYY-MM-DD, NOT D/M/YYYY — Typora
  reformats dates and earlier code assumed D/M/YYYY incorrectly)
- Derives `timestamp` and `datetime` from official UTC market open via
  exchange_calendars — NOT from the CSV (which has no time)
- Clips rows before cal.first_session; warns with count; raises if all clipped
- Pads missing trading sessions with zero volume, prices carried forward
  (ffill). Weekends and exchange holidays are excluded by exchange_calendars.
- exchange_calendars XNYS only goes back to 2006-06-30. Earlier data is
  clipped. This is intentional.

**csv2pandas_intraday(code: str, csv_path: Path, interval: str) -> pd.DataFrame**
- interval: "1m", "5m", "1h" etc.
- EODHD intraday CSV has columns: Timestamp, Gmtoffset, Datetime, OHLCV
  Datetime is ISO format "YYYY-MM-DD HH:MM:SS" (quoted in CSV)
  Gmtoffset is always 0 (EODHD normalises to UTC). Both Gmtoffset and
  Datetime are dropped; all time info comes from Timestamp (Unix epoch).
- Derives local_date from timestamp + exchange tz (ZoneInfo)
- Pads missing bars (between first and last bar of each day) with zero
  volume, prices carried forward. Padding is per local trading day.
- Does NOT pad between days — only within each day's first-to-last bar range.

**add_local_time(pdf: pd.DataFrame) -> pd.DataFrame**
- Intraday only. Raises ValueError on daily DataFrames.
- Requires all rows to share the same exchange suffix in `code`. Raises
  ValueError on mixed suffixes with a message explaining split-add-rejoin.
- Adds local_time as "HH:MM:SS" string, inserted immediately after local_date.
- Does not mutate input — returns a new DataFrame.
- For mixed-exchange data: split by suffix, add_local_time each, concatenate.

**pandas2polars(pdf) -> pl.DataFrame**
- Converts date/local_date (Python datetime.date) to pl.Date
- datetime -> pl.Datetime("us")
- timestamp -> pl.Int64, vo -> pl.Int64
- local_time preserved as pl.Utf8 if present

**polars2pandas(df) -> pd.DataFrame**
- Inverse of pandas2polars. datetime restored to datetime64[us] (not ns).
- date/local_date restored to Python datetime.date (object dtype)
- local_time preserved as str if present

**pandas2sqlite(pdf, db, tablename)**
- db: sqlite3.Connection OR str/Path (auto-opened and closed if Path/str)
- Creates table with correct DDL if not exists
- If local_time present and not already a column, uses ALTER TABLE ADD COLUMN
- INSERT OR REPLACE for idempotency

**sqlite2pandas(db, tablename) -> pd.DataFrame**
- Detects daily vs intraday from column names (date vs local_date)
- Restores date types from TEXT
- Detects local_time column presence and includes it if present

**polars2sqlite(df, db, tablename)**
- Thin wrapper: polars2pandas → pandas2sqlite

**fetch_daily(code, api_token, from_date=None, to_date=None) -> pd.DataFrame**
- Calls EODHD /api/eod/{code} with from/to as YYYY-MM-DD strings
- Returns same schema as csv2pandas_daily
- Uses io.StringIO to pipe response through csv2pandas_daily

**fetch_intraday(code, api_token, interval, from_ts=None, to_ts=None) -> pd.DataFrame**
- EODHD intraday uses Unix timestamps for from/to, NOT ISO dates
- Max window: 120 days for 1m, 600 days for 5m, 7200 days for 1h
- Intraday data available from October 2020 only
- Returns same schema as csv2pandas_intraday

**tips(db: Database, tip_list: list[tuple[str, date]], tablename: str,
       interval: str, n1=None, n2=None)**
- Standalone function (not a Database method)
- For each (code, tip_date): fetches n1 days before through n2 days after
- Uses calendar-day window for fetch (NOT session count) to capture half-days
  that exchange_calendars omits (e.g. July 3rd before Independence Day)
- Trims using actual dates in returned data, not session counts
- INSERT OR REPLACE: safe to run as daily scheduled job
- Requires db.api_token to be set
- A per-tip fetch/store failure is logged (with a memory/disk snapshot via
  `_resource_snapshot()`) and skipped rather than aborting the whole call

**unresolved_tips(db, tip_list, tablename, interval="1d", n1=None, n2=None)
-> pd.DataFrame**
- Tips whose backfill window has fully elapsed (`tip_date + n2*2+5` calendar
  days is in the past) but whose price coverage in `tablename` is still
  short of n2 days -- the closed counterpart to daily_update.py's
  `_still_open_tips()`. Usually means the ticker was renamed, split, or
  merged (EODHD doesn't follow a ticker through those events) rather than
  an actual loss -- see `ticker_aliases` below and
  doc/DELISTING_RESEARCH_2026-09-30.md.
- Returns (not drops) these tips: `code, tip_date, actual_days,
  last_available_date` -- see doc/DESIGN_DECISIONS.md on why this is
  surfaced rather than silently filtered (survivorship bias).

### ticker_aliases: smoothing renamed / split / merged / cashed-out tickers

**Table** `ticker_aliases`: `old_code TEXT, new_code TEXT, effective_date
TEXT, ratio REAL DEFAULT 1.0, cash_price REAL, reason TEXT, PRIMARY KEY
(old_code, effective_date)`. Exactly one of `new_code`/`cash_price` must be
set (enforced by a CHECK constraint and in `add_ticker_alias()`).

**add_ticker_alias(db, old_code, effective_date, new_code=None, ratio=1.0,
cash_price=None, reason=None)**
- Records that `old_code`'s series should, from `effective_date` onward,
  resolve via `new_code` (price = `ratio * new_code`'s price) or a fixed
  `cash_price` (cash-for-scrip takeover, no successor security).
- `effective_date` is a practical cutover date (when fetching `old_code`
  directly stops being useful), which can lag the real corporate-action
  date if EODHD kept serving `old_code` for a while afterward -- record the
  real date/details in `reason`.
- Takes effect on the next fetch through `tips()`/`Database.fetch()`/
  `Database.to_pandas()` -- no separate backfill step.
- Seeded 2026-09-30 with 5 rows: GMGI.US->MRDN.US, FDP.US->DMC.US,
  VSCO.US->VSXY.US, FLGC.US->ZSTK.US (all ratio 1.0), LBRDA.US->CHTR.US
  (ratio 0.236).

### Database class

```python
Database(db_path: str|Path, api_token: str = None)
```

Context manager (`with Database(...) as db:`). Holds one open SQLite
connection for its lifetime.

**Methods:**
- `from_csv(code, csv_path, interval, tablename)` — "1d" routes to daily
- `from_pandas(pdf, tablename)`
- `from_polars(df, tablename)`
- `to_pandas(tablename, code=None, interval=None, start=None, end=None, n_days=None)`
  When code+interval+api_token all present: auto-fetches from EODHD if the
  requested range is not cached. Uses wide fetch + INSERT OR REPLACE.
  end defaults to cached_max (not date.today()) — avoids empty results when
  today's data hasn't been published yet.
  n_days uses _start_from_actual_dates (not _n_sessions_before) so half-days
  are counted correctly.
  Daily auto-fetches route through `_fetch_daily_resolved()` (ticker_aliases
  resolution) rather than calling `fetch_daily()` directly.
  code/start/end are all independently optional for a plain (non-fetching)
  read: `to_pandas(tablename)` with no other args returns the whole table.
  date_col (`date` vs `local_date`, needed when start/end are given) is
  detected from the table's actual schema via `PRAGMA table_info`, not from
  `interval` — correct even when `interval` itself wasn't passed.
- `to_polars(tablename, **kwargs)` — delegates to to_pandas
- `to_csv(tablename, csv_path, **kwargs)`
- `fetch(code, interval, tablename, from_date=None, to_date=None)`
  Daily fetches also route through `_fetch_daily_resolved()`, but only when
  both from_date and to_date are given — ticker_aliases resolution needs
  concrete dates to split at the alias boundary, so an open-ended fetch
  (from_date/to_date omitted, EODHD's own default window) falls back to a
  plain `fetch_daily()` call instead.
- `_table_exists(tablename) -> bool`
- `_date_range_in_table(tablename, code, is_daily) -> (date|None, date|None)`

### Private helpers

**_suffix(code) -> str**: extracts exchange suffix, raises ValueError if unknown
**_get_calendar(suffix) -> ExchangeCalendar**: cached via _calendar_cache dict
**_session_open_ts(cal, session) -> int**: UTC epoch of market open
**_n_sessions_before(cal, ref_date, n) -> date**: n=0 returns ref_date
**_start_from_actual_dates(all_dates, end_date, n) -> date**:
  Counts back through dates actually returned by EODHD (not calendar sessions).
  This is the correct way to compute start for n_days — it counts half-days
  that exchange_calendars omits. Used in to_pandas() after fetch.
**_interval_to_freq(interval) -> str**: "5m" → "5min", "1h" → "1h"
**_is_intraday(interval) -> bool**: interval != "1d"
**_lookup_alias(db, code) -> dict | None**: earliest `ticker_aliases` row
  for `code`, or None. Returns None (not an error) if the table doesn't
  exist yet.
**_generate_cashout_rows(code, from_date, to_date, cash_price) -> pd.DataFrame**:
  flat, zero-volume rows at `cash_price` for every real trading session in
  range — the cash-for-scrip branch of alias resolution.
**_fetch_daily_resolved(db, code, api_token, from_date, to_date) -> pd.DataFrame**:
  `fetch_daily()`, but splits the requested range at any applicable
  `ticker_aliases` boundary and resolves the post-boundary segment via
  `new_code` (price-scaled by `ratio`, recursing for chained renames) or
  `_generate_cashout_rows()`. Always returns a DataFrame labelled `code`
  throughout, schema-identical to `fetch_daily()`'s own output. Daily only
  — no intraday equivalent (not needed by any current caller). Volume is
  not rescaled across an alias boundary.

### Known limitations / gaps

1. **exchange_calendars half-days**: The library does not model half-day
   sessions (e.g. July 3rd before Independence Day). The fetch window uses
   calendar-day buffers to work around this, but padding logic still skips
   half-days (no padded rows are added for them). The data IS stored if
   EODHD returns it; it just won't be padded if missing.

2. **exchange_calendars coverage start**: XNYS starts 2006-06-30. Pre-2006
   daily data is clipped and warned about. This is by design — timestamps
   before that date cannot be reliably derived. EODHD from/to parameters
   should be used to avoid downloading pre-2006 data in the first place.

3. **v2.0 lazy fetch not complete**: tips() and Database.to_pandas() will
   auto-fetch from EODHD when data is missing, but there is no URL
   construction for arbitrary historical gaps. The plan is to use the
   timestamp column (which holds UTC market open epoch) to construct EODHD
   download URLs for missing date ranges. This is explicitly deferred.

4. **Only .US suffix tips**: tips_io.py appends ".US" to all tickers. The
   tipping service (StockDataAnalytics) only covers US stocks.

---

## tips_io.py — full specification

### Purpose
Parses StockDataAnalytics daily tip emails (.eml format) into two DataFrames
and SQLite tables. No dependency on eodhd_io.py.

### Card format
Every captured email (April 2026 through the current archive — no exceptions
found across 166 emails) uses the same layout: tips 1-3 use the "full-detail"
card format (printed number + score bar), tips 4-20 use the "compact" format
(bar only, no printed number). `_parse_tip_card()` selects the branch purely
positionally, via `if tip_n <= 3:` — there is no HTML-based format
discriminator (e.g. checking for a specific font-size or element type). If a
future email ever puts a different number of tips in the full-detail format,
this positional check would need to change.

### Card anchor
Each tip card is a `<td>` with `border-bottom: 1px solid` in its style,
containing exactly one unique stockdataanalytics.com/news/ link.
`_find_card_td(a_tag)` walks up from the link's `<a>` tag to find this td.
Unique links are deduplicated by URL path (stripping query string).

### Full card fields extracted
code (ticker + ".US"), url, name, sector, win_probability (int),
entry_zone_low, entry_zone_high, target, stop (all float),
expected_reward (float, positive), expected_risk (float, NEGATIVE),
holding_period_low, holding_period_high (int),
pattern_quality_score, setup_score, risk_reward_score, context_score (float),
pattern_quality_colour, setup_colour, risk_reward_colour, context_colour (int 1-4)

### Compact card fields extracted
Same fields EXCEPT: name is None. Order: PQ, Setup, R:R, Context.
Reward/risk/hold come from a single metrics paragraph.

The four `..._score` fields ARE populated for compact cards too (as of the
2026-09-12 unification, see `doc/DESIGN_DECISIONS.md`) — but as an *estimate*
reconstructed from the score bar's filled pixel height, not a printed number
(the compact HTML never shows one). Precision is roughly +/-0.75-1.7 points
depending on category — see `doc/LIMITATIONS.md`. Bar colours are read from
the bar's fill background colour directly (not from a number's text colour,
since there is no number to read the colour off of).

### Score scale and colour scheme
Each of the four qualities has its own maximum, confirmed against every
captured email's own "Total Score: X / 98" (`CATEGORY_MAX` in tips_io.py):
- pattern_quality: 40
- setup: 20
- risk_reward: 18
- context: 20  (sums to 98)

Colour is an integer traffic-light scale (stored in _COLOUR_INT):
- 1 = green   (#22c55e)
- 2 = yellow  (#eab308, #ca8a04)
- 3 = orange  (#f97316)
- 4 = red     (#ef4444)

The colour bucket is a fixed function of `score / category_max`, identical
across all four qualities and both card formats (verified against all
13,040 score bars in the captured corpus, zero exceptions):
- >= 0.75  -> green
- 0.50-0.74 -> yellow
- < 0.50   -> orange (red never observed on a per-tip quality score)

The parser cross-checks the colour it read from the HTML against the colour
this rule implies from the score, and logs a warning (does not raise) on any
mismatch — see `_check_colour_consistency()`.

### expected_risk sign convention
expected_risk is always stored as a POSITIVE number (the absolute value of
the loss). The full card HTML shows "-$0.52" and the code stores 0.52.
The compact card HTML shows "-$0.18" and the code stores 0.18.
This applies to both full and compact card formats.

### tip_n
1-based position within the email (order of first appearance in HTML,
after deduplication). Not explicitly in the HTML.

### SQLite tables
**tip_exchange**: PRIMARY KEY (exchange, tip_date)
**tip_details**: PRIMARY KEY (exchange, tip_date, tip_n)
Both use INSERT OR REPLACE. Colour columns are INTEGER (nullable Int64 in pandas).

### Public functions
- `parse_tip_email(eml_path) -> (exchange_df, tips_df)`
- `parse_tip_emails([paths]) -> (exchange_df, tips_df)` — concatenated
- `tips_exchange2sqlite(exchange_df, tips_df, db, exchange_tablename, tips_tablename)`
- `tips_sqlite2pandas(db, exchange_tablename, tips_tablename, start, end)`

---

## scripts/daily_update.py — nightly automation

Run by three separate systemd timer/service pairs in `ops/`
(`eodhd-daily-update.timer`, `-1d.timer`, `-5m.timer`) via
`ops/run_daily_update.sh`, which pulls the canonical db from remote storage
(rclone), runs this script, and pushes it back. The script itself is
storage-agnostic — it just reads/writes whatever `--db-path` points at.

`--mode {tips-only,daily-price,intraday-price}` (required) selects what a
run actually fetches — see `doc/DESIGN_DECISIONS.md`
("weekday/Saturday/Sunday split"):
- **tips-only** (weekdays, `eodhd-daily-update.timer`): parses emails only,
  no price fetch at all.
- **daily-price** (Saturdays, `eodhd-daily-update-1d.timer`): also fetches
  daily OHLCV into `PRICE_TABLENAME` ("daily").
- **intraday-price** (Sundays, `eodhd-daily-update-5m.timer`): also fetches
  5-minute OHLCV into `INTRADAY_TABLENAME` ("intraday_5m").

Each run:
1. Downloads unseen tip emails (`email_downloader.download_emails`,
   `unseen_only=True`) — every mode does this, since it's cheap and the
   whole point of the weekday runs.
2. If any arrived, parses them and writes `tip_exchange` / `tip_details`
   (`parse_tip_emails`, `tips_exchange2sqlite`).
3. If `--mode` is `tips-only`, stops here (after step 5 below) — no price
   fetch. Otherwise, builds `tip_list` for `tips()` from **two** sources,
   unioned: tips parsed just now, plus every `(code, tip_date)` already in
   `tip_details` with `tip_date >= date.today() - (n2*2+5)` calendar days
   (`_still_open_tips()`). The cutoff mirrors the calendar-day buffer
   `tips()` uses internally, so it's exactly as generous as the window
   `tips()` will actually try to fill. The union is sorted by
   `(tip_date, code)` before use -- a plain `set`'s iteration order is
   hash-based and scrambles from run to run, which makes the log (and
   diagnosing a failure from it) much harder to follow.
4. Calls `tips()` once with the sorted list and the tablename/interval for
   the current `--mode` (`MODE_PRICE_CONFIG`), if the list is non-empty
   (logs and skips otherwise).
5. Calls `_log_unresolved_tips(db)`: builds the *full* tip history via
   `_all_tips()` (unlike `_still_open_tips()`, this is not cutoff-filtered
   -- `unresolved_tips()` specifically needs tips whose window has already
   closed) and calls `eodhd_io.unresolved_tips()` against it. Logs a
   `WARNING` listing every flagged tip (code, tip_date, actual_days,
   last_available_date) if any, else an `INFO` "no unresolved tips" line.
   Runs every time regardless of `--mode` (including `tips-only`, and
   regardless of whether step 4 had anything to do), so this surfaces on its
   own rather than needing someone to remember to run `unresolved_tips()`
   manually. See `doc/DESIGN_DECISIONS.md` ("ticker_aliases...") for what to
   do with a flagged tip. Currently only checks `PRICE_TABLENAME`/
   `PRICE_INTERVAL` ("daily"/"1d") -- intentionally not yet extended to
   `intraday_5m` (see `doc/DESIGN_DECISIONS.md`,
   "weekday/Saturday/Sunday split").

This backfill step exists because the newsletter arrives before US market
open: the first time a brand-new tip is seen, `tips()` can only fetch the n1
days *before* tip_date — the n2 days *after* don't exist in EODHD yet.
Without re-requesting a tip on later runs, that post-tip-date window (the
part needed to score the tip's outcome) would never get filled in. See
`doc/DESIGN_DECISIONS.md` ("daily_update.py re-requests still-open tips") for
the full rationale, including why this uses a calendar-day cutoff rather than
checking actual row coverage in the price table.

## Dependencies

```
pandas
polars
pyarrow          # required for polars<->pandas bridge
exchange_calendars
requests
beautifulsoup4
lxml             # HTML parser for bs4 (or substitute "html.parser")
```

Python 3.9+ required (uses zoneinfo stdlib). Tested on Python 3.14.2.

---

## Test suite (test_eodhd.py)

pytest with custom `--eml` option for email tests.
Markers: `eml` (requires --eml path), `network` (requires EODHD_API_TOKEN).

Run offline tests only:
```
pytest test_eodhd.py -v -m "not network and not eml"
```

Run with April email:
```
pytest test_eodhd.py -v --eml path/to/2026-04-08_Daily_Stock_Pick.eml
```

The June 2026 email tests (TestTipsIoJune) require the June email specifically.
April and June test classes skip themselves if the wrong email is supplied.
To run both, you would need to run pytest twice with different --eml arguments,
or extend the fixture to accept multiple email paths.

### Key test assertions to know
- expected_risk is always <= 0
- STRO (April tip 1): entry_zone_low=25.58, target=27.00, stop=22.62,
  expected_reward=0.78, expected_risk=-0.52
- BNAI (June tip 4, compact): entry_zone_low=21.02, target=22.08, stop=20.19,
  expected_reward=0.51, expected_risk=-0.18
- datetime dtype is datetime64[us] (not ns) throughout
- date/local_date is Python datetime.date (object dtype in pandas)
