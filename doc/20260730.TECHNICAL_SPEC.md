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
DEFAULT_N1 = 3   # trading days before tip date for tips()
DEFAULT_N2 = 10  # trading days after tip date for tips()
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
- `to_polars(tablename, **kwargs)` — delegates to to_pandas
- `to_csv(tablename, csv_path, **kwargs)`
- `fetch(code, interval, tablename, from_date=None, to_date=None)`
- `_table_exists(tablename) -> bool`
- `_date_range_in_table(tablename, code, is_daily) -> (date|None, date|None)`

### Private helpers

**_suffix(code) -> str**: extracts exchange suffix, raises ValueError if unknown
**_get_calendar(suffix) -> ExchangeCalendar**: cached via _calendar_cache dict
**_session_open_ts(cal, session) -> int**: UTC epoch of market open
**_n_sessions_before(cal, ref_date, n) -> date**: n=0 returns ref_date
**_n_sessions_after(cal, ref_date, n) -> date**
**_start_from_actual_dates(all_dates, end_date, n) -> date**:
  Counts back through dates actually returned by EODHD (not calendar sessions).
  This is the correct way to compute start for n_days — it counts half-days
  that exchange_calendars omits. Used in to_pandas() after fetch.
**_interval_to_freq(interval) -> str**: "5m" → "5min", "1h" → "1h"
**_is_intraday(interval) -> bool**: interval != "1d"

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

5. **Exchange suffix validation in to_pandas()**: The date_col detection uses
   PRAGMA table_info rather than the interval parameter. This means if
   interval is not passed, the correct date column is still detected.

---

## tips_io.py — full specification

### Purpose
Parses StockDataAnalytics daily tip emails (.eml format) into two DataFrames
and SQLite tables. No dependency on eodhd_io.py.

### Email format history
The email HTML structure changed between April 2026 and June 2026:
- April 2026: ALL 20 tips use the "full-detail" card format
- June 2026+: tips 1-3 use full-detail; tips 4-20 use "compact" format

The parser detects which format each card uses and delegates accordingly.
New email formats may require updating the parser.

### Card format detection
`_is_full_card(card_td) -> bool`:
Full cards have a `<p style="font-size: 32px">` for win probability.
Compact cards use a `<span style="font-size: 14px; font-weight: 800">` inside
a coloured circle div. The 32px check is the discriminator.

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
pattern_quality_number, setup_number, risk_reward_number, context_number (float),
pattern_quality_colour, setup_colour, risk_reward_colour, context_colour (int 1-4)

### Compact card fields extracted
Same fields EXCEPT: name is None, and pattern/setup/rr/context NUMBER fields
are None. Bar colours are derived from the visual bar fill background colour
(not from a number's text colour). Order: PQ, Setup, R:R, Context.
Reward/risk/hold come from a single metrics paragraph.

### Colour scheme
Integer traffic-light scale (stored in _COLOUR_INT):
- 1 = green   (#22c55e)
- 2 = yellow  (#eab308, #ca8a04)
- 3 = orange  (#f97316)
- 4 = red     (#ef4444)

### expected_risk sign convention
expected_risk is always stored as a NEGATIVE number (it is a loss).
The full card HTML shows "-$0.52" and the code stores -0.52.
The compact card HTML shows "-$0.18" and the code stores -0.18.
Earlier versions of the code incorrectly stored it as positive for full cards.
If you see positive expected_risk values, check the sign convention.

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

### Sign Convention
- `expected_risk` is stored as a **positive number** (representing the absolute risk amount).
- This applies to both full and compact card formats.
---

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
