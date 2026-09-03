# Design Decisions Record

This document records the significant design choices made during development,
the reasoning behind them, and known open questions. It is intended to help
a new LLM instance avoid re-litigating settled decisions and understand
which areas are genuinely open.

---

## Architecture

### Single SQLite file, caller-specified table names
**Decision:** All price data (daily, intraday of any interval, multiple
instruments) goes into one SQLite file. Table names are a business decision
passed by the caller — not encoded in the library.

**Reasoning:** Keeps the library policy-free. The caller decides whether to
segregate by interval, by exchange, or by use case. The library just writes
to whatever table name it receives.

**Example caller convention (not enforced by library):**
`daily`, `intraday_5m`, `intraday_1m`, `tip_test_1d`

### Free functions + thin Database class
**Decision:** Core logic lives in standalone free functions
(csv2pandas_daily, pandas2sqlite, etc.). The Database class is a thin wrapper
that holds a connection and tablename and delegates to the free functions.

**Reasoning:** Free functions are independently testable and composable.
The Database class provides convenience without locking logic inside it.
The free functions accept either an open sqlite3.Connection or a file path —
this gives maximum flexibility while the Database class handles the common case.

### tips_io.py is a separate file
**Decision:** Email parsing code lives in tips_io.py, not eodhd_io.py.

**Reasoning:** Different concern (HTML parsing vs data I/O), different
dependencies (beautifulsoup4, email), different data model. The only coupling
is that the tips() function in eodhd_io.py takes a Database instance.
tips_io.py has zero imports from eodhd_io.py.

---

## Data model

### datetime is always UTC tz-naive
**Decision:** The `datetime` column stores UTC time as a tz-naive
datetime64[us]. `date` (daily) and `local_date` + `local_time` (intraday)
carry the local exchange representation separately.

**Reasoning:** Mixing tz-aware and tz-naive datetimes in pandas/polars/SQLite
causes constant friction. UTC tz-naive is unambiguous if documented. The
local columns give the human-readable view without muddying the authoritative
timestamp.

**Consequence:** You cannot use `datetime` directly for display — always use
`local_date`/`local_time` for intraday and `date` for daily.

### datetime64[us] not [ns]
**Decision:** All datetime columns use microsecond precision (us), not the
pandas default nanoseconds (ns).

**Reasoning:** Polars uses us internally. The round-trip pandas→polars→pandas
previously broke because pandas used ns and polars used us, producing unequal
DataFrames. Standardising on us makes round-trips lossless.

### Primary key is (code, timestamp)
**Decision:** Both daily and intraday tables use (code, timestamp) as primary
key, not (code, date) or (code, local_date).

**Reasoning:** timestamp is an integer epoch and is unambiguous for any
timezone. date/local_date are derived. For intraday data there is no date-only
primary key that works across timezones. For daily data, timestamp is derived
from the market open, which is also unique per (code, session).

### Padded rows use zero volume
**Decision:** Missing trading sessions (daily) and missing bars (intraday)
are padded with vo=0 and prices carried forward from the most recent real bar.

**Reasoning:** Zero volume is an unambiguous marker for synthetic rows.
Forward-fill of prices is the standard convention for gap-filling in
backtesting — the last traded price is the best estimate of "fair value"
during a no-trade period.

**Gap for consideration:** There is no flag column marking padded vs real rows.
If the calling code needs to distinguish them, it must check vo == 0 and
reason about whether that's a genuine zero-volume session or a padded one.
A boolean `is_padded` column was considered and not implemented.

### No adjusted close for intraday
**Decision:** The `ac` (adjusted close) column only appears in daily DataFrames.

**Reasoning:** EODHD does not provide adjusted prices at intraday frequency.
The adjustment factors are only meaningful at the daily level.

### local_time is optional
**Decision:** `local_time` is not added by csv2pandas_intraday. It requires
an explicit call to add_local_time().

**Reasoning:** local_time is a display convenience (for DBBrowser). It is
redundant — derivable from timestamp + exchange timezone. Not everyone wants
the extra column. The function is separate so it can be applied selectively.

**Constraint:** add_local_time() requires all rows to share the same exchange
suffix. Mixed-exchange DataFrames must be split, processed, and recombined.
This is documented as the intended workflow, not a bug.

---

## Exchange and timezone handling

### Timezone implicit in exchange suffix
**Decision:** The exchange suffix (e.g. ".LSE", ".US") carries the timezone
implicitly. There is no separate timezone column.

**Reasoning:** The suffix is already stored in the `code` column. Adding a
`timezone` column would be redundant. The EXCHANGE_INFO dict maps suffix to
IANA timezone. The caller is expected to have homogeneous tables (one exchange
per table) or to handle the split-recombine pattern explicitly.

### exchange_calendars for timestamps, not for raw market hours
**Decision:** Market open timestamps come from exchange_calendars schedule
(which gives actual UTC open per session), not from manually configured
open/close times.

**Reasoning:** exchange_calendars correctly handles DST transitions and
variations. Manual open times are stored in EXCHANGE_INFO for documentation
only — they are not used in any calculation.

**Exception:** exchange_calendars does not model half-day sessions (e.g.
July 3rd before Independence Day). These are treated as non-sessions by the
library even though NYSE trades on them. The workaround is to use
calendar-day windows for EODHD fetches and trim using actual returned dates.

### Pre-2006 data is clipped
**Decision:** Daily rows before the calendar's first_session are clipped with
a warning, not errored or silently dropped.

**Reasoning:** exchange_calendars XNYS only goes back to 2006-06-30. Without
a session schedule, we cannot derive a reliable UTC market open timestamp for
earlier dates, and the timestamp column is important for future v2.0 URL
construction. Silently storing pre-2006 rows with midnight UTC timestamps
would be misleading. The warning informs the user that data was dropped.

**Practical mitigation:** EODHD's from_date parameter can limit downloads to
post-2006 data, making the clip rarely necessary once v2.0 lazy fetch is built.

---

## Caching / fetch strategy

### Wide fetch, trim after
**Decision:** When fetching from EODHD to populate the cache, use a generous
calendar-day window and let INSERT OR REPLACE handle any overlap.

**Reasoning:** Calculating the exact EODHD from/to parameters to fetch
precisely the missing rows is complex (requires knowing which days the
exchange was open, accounting for half-days, etc.). Over-fetching is cheap;
duplicates are handled by the database. This approach is robust and simple.

### end defaults to cached_max, not today
**Decision:** When n_days is specified in to_pandas() without an explicit end
date, end resolves to the latest date already in the table (cached_max), not
date.today().

**Reasoning:** Today's data may not have been published yet (market not open,
holiday, EODHD processing lag). Defaulting to today causes n_days=1 to return
zero rows when today's data is absent. Defaulting to cached_max means n_days
always returns the last n days of data that actually exists.

### _start_from_actual_dates for n_days computation
**Decision:** After fetching, the start date for the n_days window is computed
by counting back through dates actually present in the table, not through
exchange_calendars sessions.

**Reasoning:** exchange_calendars skips half-days. If we use session counts to
compute start, half-days are not counted and the user gets fewer rows than
requested. Counting actual table dates means half-days count naturally.

---

## tips() design

### tips() is a free function, not a Database method
**Decision:** tips() takes a Database instance as a parameter rather than
being a method on Database.

**Reasoning:** tips() has a distinct purpose (populating event-anchored data)
versus the general caching purpose of Database. Making it a free function
keeps Database simpler and makes tips() independently importable.

### tip_date is the entry date
**Decision:** tip_date is the date the newsletter email arrives, which is also
the recommended entry date (Ian receives the email a few hours before US
markets open).

**Reasoning:** The newsletter delivers tips pre-market on the entry day.
n1 days before = context leading up to the tip. n2 days after = measuring
outcomes. Day 0 is tip_date itself.

**Implication:** n1=3, n2=10 gives 14 trading days of data total (3 before,
tip day, 10 after). This is the intended use for backtesting.

### Calendar-day buffer for end_date in tips()
**Decision:** The fetch window end is tip_date + n2*2 + 5 calendar days, not
the nth session after tip_date.

**Reasoning:** Same half-day issue as above. The buffer is always larger than
needed; the actual n2 days are determined by counting real dates returned.

---

## tips_io design

### All tickers appended with ".US"
**Decision:** tips_io.py always appends ".US" to ticker codes.

**Reasoning:** StockDataAnalytics only covers US stocks. There is no exchange
indicator in the email HTML.

**Risk:** If the newsletter ever adds non-US stocks, this will silently produce
wrong codes. No defensive check exists for this.

### Two-format parser
**Decision:** The parser detects full-detail vs compact card format per card
and delegates to separate _parse_tip_card_full() and _parse_tip_card_compact()
functions.

**Reasoning:** The email format changed between April 2026 (all full) and June
2026 (3 full + 17 compact). A single function with branching would become
unreadable. Separate functions make the difference explicit and each is
independently testable.

**Risk:** Future email format changes will require updating the parser. The
format is controlled by the newsletter provider and can change without notice.
The test suite includes specific value assertions against real emails which
will catch format changes.

### Numeric score values absent from compact cards
**Decision:** pattern_quality_number, setup_number, risk_reward_number,
context_number are None for compact cards. Only bar colours are available.

**Reasoning:** The compact card format does not include numeric scores, only
visual bar charts. The bar height is relative and carries no numeric meaning
extractable from the HTML. Only the fill colour is reliable.

**Implication for backtesting:** If numeric scores are needed for all 20 tips,
the user must follow the URL to the detail page. This is a data gap that cannot
be resolved from the email alone.

---

## v2.0 deferred features

These were discussed and explicitly deferred:

1. **Lazy fetch / automatic URL construction**: Database.to_pandas() currently
   auto-fetches missing data, but the v2.0 vision is for the database to be
   fully self-populating — given a code and a date range, it constructs the
   right EODHD URLs and fetches exactly what's missing. The timestamp column
   (UTC market open epoch) is designed to support this. The human wants to
   validate that timestamps produce correct download URLs before building this.

2. **tips() called as daily scheduled job**: The design supports this (INSERT
   OR REPLACE, idempotent), but the scheduler itself is not part of the
   library.

3. **Backtesting logic**: The pipeline feeds data into a backtesting workflow
   but no backtesting code exists yet. tips() and the OHLCV fetch are the
   data preparation layer.

---

## Open design questions

1. **Multiple emails for test_eodhd.py**: Currently the --eml option accepts
   one email. To run both April and June tests in one pytest invocation, the
   fixture would need to accept a list. Not yet implemented.

2. **Pre-2006 timestamp strategy**: If the human ever needs pre-2006 daily
   data (e.g. for long-horizon backtests), a fallback strategy for timestamp
   derivation is needed — either midnight UTC or some other convention.
   Currently the data is simply dropped.

3. **Non-US tips**: If StockDataAnalytics ever adds LSE or ASX tips, the
   ".US" hardcoding in tips_io.py needs to change. The email HTML would need
   to be inspected for exchange indicators.

4. **Half-day padding**: Currently no padded rows are generated for half-day
   sessions (e.g. July 3rd). If the EODHD data for that day is missing, there
   is no way to detect that it's a half-day and generate appropriate padded
   rows (since exchange_calendars doesn't model them). This is an accepted gap.

5. **is_padded flag**: No boolean column distinguishes padded from real rows.
   Users rely on vo == 0 as a proxy. This is fragile for instruments that
   genuinely trade at zero volume. A proper solution would be an `is_padded`
   boolean column, but this would require schema changes.
