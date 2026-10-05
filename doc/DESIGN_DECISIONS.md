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

### to_pandas() without code silently returned zero rows (2026-10-05)
**Problem:** `to_pandas()`/`to_polars()` always built `WHERE code = ?` with
`params=[code]`, even when `code` wasn't passed (it's documented as only
required for the auto-fetch branch, not for a plain read). SQL's `x = NULL`
never matches, so any code-less call -- e.g. `to_polars(tablename)` to read
or diff a whole table, exactly what `scripts/compare_db.py` needed to do --
silently returned an empty DataFrame. No error, just nothing, which is a
much worse failure mode than a loud one: it looks like "no data" rather
than "wrong query." Found via Ian's own db-comparison script reporting zero
rows against tables that genuinely had 30k+ rows each.

A second, dormant bug lived in the same block: `date_col` defaulted to
`"local_date"` (the intraday column) whenever `interval` wasn't passed --
untriggered only because the code-less calls that hit this also happened
to omit `start`/`end`. `doc/TECHNICAL_SPEC.md`'s "Known limitations" used
to claim this was already handled via `PRAGMA table_info` -- it wasn't;
that described `sqlite2pandas()`'s behavior, a different function, not
`to_pandas()`'s.

**Decision:** the `code` filter is now applied only when `code` is given,
and `date_col` is derived from the table's actual schema (`PRAGMA
table_info`) instead of trusted from `interval` -- matching the detection
`sqlite2pandas()` already did correctly. Both now behave correctly
independent of which of `code`/`interval`/`start`/`end` are supplied.

**Reasoning:** `to_pandas(tablename)` with no other args is a reasonable,
already-documented call shape (reading/diffing a whole table), and should
not require a dummy `code` just to avoid a silent empty result.

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

**Implication:** n1=20, n2=20 gives 41 trading days of data total (20 before,
tip day, 20 after). This is the intended use for backtesting.

### daily_update.py re-requests still-open tips (2026-09-26)
**Problem:** `tips()` is idempotent and safe to call repeatedly, but
`scripts/daily_update.py` only ever called it with tips parsed from *that
run's newly-downloaded* emails (`unseen_only=True` means each email is "new"
exactly once). Combined with the fact that the newsletter arrives before
market open, the very first (and, under the old code, only) call for a given
tip happens before that tip's own trading day exists in EODHD -- so
`actual_after` inside `tips()` is empty and zero post-tip-date rows are ever
fetched, for every tip, indefinitely. The n1-before context was fine; the
n2-after data -- the part actually needed to score a tip's outcome -- was
never backfilled by any later run.

**Decision:** every run of `daily_update.py` now builds its `tip_list` from
two sources: tips parsed from today's new emails, plus any tip already in
`tip_details` whose `tip_date` is within the last `n2 * 2 + 5` calendar days
(the same buffer `tips()` itself uses internally for its fetch window --
see `_still_open_tips()` in `scripts/daily_update.py`). `tips()`'s existing
idempotent re-fetch + `INSERT OR REPLACE` then naturally fills in one more
real trading day per run until the window is complete.

**Alternative considered:** instead of a calendar-day cutoff on `tip_date`,
check actual row coverage in the price table (`COUNT(*) WHERE date >
tip_date`) and only re-include a tip while that count is `< n2`. This is more
precise (stops re-fetching the instant a tip's window is genuinely resolved)
but was rejected for now: the price table can contain padded rows (vo=0,
forward-filled -- see "Padded rows use zero volume" above), so a naive row
count doesn't necessarily match what `tips()` itself considers "done" (which
counts dates actually returned by EODHD, pre-padding). Correctly reproducing
that logic outside `tips()` is nontrivial, whereas the calendar-day cutoff
matches the "wide fetch, trim after" philosophy already used everywhere else
in this codebase (see below) and only costs a handful of harmless, idempotent
re-fetches for tips whose window finished early within the buffer period. If
EODHD call volume ever becomes a real constraint, the row-coverage check
could be layered on top as an optimization rather than replacing this.

### tips() per-tip failure handling (2026-09-29)
**Problem:** The first real production run of `daily_update.py` (a ~1550-tip
backlog) crashed partway through: one `fetch_daily` call got back a `200 OK`
whose body wasn't the expected CSV, which surfaced several function calls
and a re-serialization later as a confusing "missing columns" `ValueError`
with no context left about the actual HTTP response -- and, because
`tips()`'s loop had no error handling, that one bad response aborted the
entire run, losing progress on every other tip still queued.

**Decision:** three changes to `tips()`/`_eodhd_fetch_csv()` in
`src/eodhd_io.py`:
1. `tips()`'s per-tip loop wraps the fetch/trim/write in `try/except
   Exception`: a failure is logged (code, tip_date, exception, and a
   memory/disk snapshot from `_resource_snapshot()`) and skipped, not
   raised. The design already tolerates partial progress (idempotent,
   re-requested via `_still_open_tips()`), so this just extends that
   tolerance to unexpected failures, not only "not enough days yet."
2. `_eodhd_fetch_csv()` validates the response has the expected columns
   right where the HTTP status/body are still available, raising a new
   `_TransientEodhdResponse` (a `ValueError` subclass) with the redacted
   URL, status, and a body snippet -- instead of the generic check deep
   inside `csv2pandas_daily`, several calls removed from the response.
3. `_eodhd_fetch_csv()` retries once (short fixed backoff) specifically on
   `_TransientEodhdResponse`, in case the bad response was transient. A
   permanently bad request (bad ticker, bad token) still fails within two
   attempts rather than looping.

**Reasoning:** An unattended nightly job over hundreds of tips should never
let one bad response abort everything else -- the cost of over-tolerance
(a skipped tip, retried next run) is far lower than the cost of
under-tolerance (the whole night's progress lost). Root-causing the actual
crash afterwards (see "unresolved_tips() surfaces permanently-short tips
instead of dropping them" below) showed the bad responses were delisted
tickers, not a rate limit as originally suspected -- exactly the kind of
thing `_resource_snapshot()` and the redacted-URL/status/body diagnostics
were meant to make traceable rather than requiring after-the-fact log
archaeology.

### Calendar-day buffer for end_date in tips()
**Decision:** The fetch window end is tip_date + n2*2 + 5 calendar days, not
the nth session after tip_date.

**Reasoning:** Same half-day issue as above. The buffer is always larger than
needed; the actual n2 days are determined by counting real dates returned.

### unresolved_tips() surfaces permanently-short tips instead of dropping them (2026-09-30)
**Problem:** Once tips() and daily_update.py's per-tip resilience (see
"tips() per-tip failure handling" above) were in place, a real production
run surfaced several tips (GMGI.US, FDP.US, VSCO.US, FLGC.US, LBRDA.US)
whose post-tip-date price window wasn't filling in -- EODHD had stopped
returning data for these tickers shortly after their tip date. Any future
analysis/backtesting code written against `n1=n2=20` assuming every tip has
a full window would break on these, or silently need to filter them out
somehow. (Researching these afterwards showed the data stopping is usually
*not* a delisting/loss -- see "ticker_aliases" below, added the same day --
but `unresolved_tips()` itself doesn't assume either way; it just measures
coverage.)

**Decision:** `unresolved_tips(db, tip_list, tablename, interval, n1, n2)`
identifies tips whose backfill window has fully closed (same
`tip_date + n2*2+5` calendar-day buffer as `_still_open_tips()`/`tips()`
use) but still have fewer than `n2` days of price coverage, and returns
them (code, tip_date, actual_days, last_available_date) rather than
dropping them from any table.

**Reasoning:** Silently dropping or moving these tips to a separate,
rarely-consulted table would be survivorship bias -- the industry-standard
pitfall in backtesting, where systematically excluding delisted/failed
securities makes results look better than reality, precisely because the
excluded securities are disproportionately the failures. Professional
databases (e.g. CRSP) handle this by keeping delisted securities with a
delisting code/return rather than removing them. Given this project doesn't
have delisting-reason data from EODHD, `unresolved_tips()` takes the
lighter-weight equivalent: keep everything in one table, and give analysis
code a function it must explicitly call to find (and consciously decide how
to handle) the unresolved set, rather than a default that silently filters
them out.

**Why not a persisted status column:** Considered (Ian's original proposal:
an integer error column, 0=good/1=delisted-empty/2=delisted-short) but
rejected in favour of computing this on demand, consistent with this
codebase's existing free-functions-over-stored-state philosophy
(`_still_open_tips()` also computes on demand rather than caching). A
persisted column needs a write path to stay correct and would need to
classify *which* EODHD failure shape occurred (empty `Value` response vs.
a short-but-valid CSV, as GMGI/FDP/VSCO/FLGC vs. LBRDA respectively) --
`unresolved_tips()` avoids that classification entirely by checking the
same underlying fact (is there still less than n2 days of coverage once
the window has closed) that both failure shapes have in common, and will
handle any other future failure shape the same way without changes.

### ticker_aliases: smoothing renamed / split / merged / cashed-out tickers (2026-09-30)
**Problem:** Researching the five tips `unresolved_tips()` surfaced (see
`doc/DELISTING_RESEARCH_2026-09-30.md`) found that EODHD doesn't follow a
ticker through a rename, reverse split, or merger -- its price series for
the old symbol just stops, with no pointer to the successor symbol. None of
the five was actually a loss: four were pure renames/splits with full value
continuity (GMGI/FDP/VSCO/FLGC), one was a completed stock-for-stock merger
paid at fair value (LBRDA -> 0.236 CHTR shares/share). Ian also raised a
third case worth planning for even though none of the five needed it: a
pure cash-for-scrip takeover, where the position is closed for a fixed cash
amount with no successor security at all.

**Decision:** a `ticker_aliases` table (`old_code, new_code, effective_date,
ratio, cash_price, reason`, added/edited via `add_ticker_alias()`) plus
`_fetch_daily_resolved()`, which `tips()`, `Database.fetch()`, and
`Database.to_pandas()` now call instead of `fetch_daily()` directly for
daily data. It resolves in one of two ways from `effective_date` onward:
- **`new_code` set** (rename/split/merger): fetch `new_code`'s price
  series, multiply price columns (op/hi/lo/cl/ac -- not volume, see below)
  by `ratio`, and label the result `old_code`. Chains automatically (a
  ticker renamed twice resolves through `new_code`'s own alias).
- **`new_code` NULL, `cash_price` set** (cash-for-scrip): no successor
  security to splice against, so generate flat rows at `cash_price` with
  `vo=0` for every real trading session from `effective_date` onward --
  reusing the existing "padded rows use zero volume" convention (see
  above) for a closed-out position instead of a missing trading session.

Either way the result is schema-identical to `fetch_daily()`'s own output
and materialized into the *original* ticker's rows via the normal
`pandas2sqlite()` write path -- no downstream code (`to_pandas`,
`sqlite2pandas`, raw SQL, DB Browser) needs to know an alias was involved,
which was the explicit goal ("calling code doesn't need to know all the
hairy detail, it just needs to see appropriate prices").

**Reasoning for resolving at fetch time, not query time:** the alternative
(a SQL view or read-time union) would need every existing read path taught
about aliasing. Resolving once, at the point new data enters the system,
and writing it under the original ticker keeps every other function
(most of them written before aliasing existed) completely unchanged.

**Reasoning for `effective_date` being a practical cutover, not necessarily
the real corporate-action date:** GMGI.US is the case that needed this --
the real rename was 2026-03-03, but EODHD kept serving (already
split-adjusted) GMGI.US data through 2026-05-04, so `effective_date` is set
to 2026-05-05 to use the real data as long as it's actually being served.
The true event date/details belong in `reason` for human reference; only
`effective_date` drives resolution.

**Why volume isn't rescaled:** a split/merger's share-count change doesn't
translate meaningfully into "old ticker's volume" (concretely: LBRDA's
"volume" after the merger would require rescaling CHTR's real trading
volume by `1/ratio`, which is meaningless -- CHTR's volume reflects CHTR's
own much larger float, not a hypothetical LBRDA-equivalent one). Only price
continuity was the stated goal, so only price columns are scaled.

**How aliases get added:** manually, via `add_ticker_alias()`, the same way
the five above were researched and added -- this isn't the kind of thing to
auto-detect (no delisting-reason data from EODHD). Once added, resolution
is automatic and retroactive-feeling without any backfill script: the next
normal fetch through `tips()`/`to_pandas()` for that ticker just picks up
the gap, since the fetch boundary itself is now alias-aware.

**Discovery is automatic even though fixing isn't (2026-09-30):**
`daily_update.py` calls `_log_unresolved_tips()` at the end of every run
(against the *full* tip history via `_all_tips()`, not just recent tips --
`unresolved_tips()` needs tips whose window has already closed, which
`_still_open_tips()` deliberately excludes), logging a `WARNING` listing
anything still flagged. Before this, the five tickers above were only
found because Ian happened to notice something odd in the log and asked
about it -- there was no mechanism that would have surfaced them on its
own. A genuine, confirmed total loss (real bankruptcy, no successor, no
payout) can also be recorded via `add_ticker_alias(..., cash_price=0.0,
reason="confirmed bankruptcy")` so it stops being re-flagged every run
once it's been researched and decided, the same as any other alias.

**Effect on `unresolved_tips()`:** not superseded -- its role narrows. A
tip it flags is now either a candidate needing alias research, or (once
researched and no alias applies) a genuine unrecoverable loss. Once an
alias is added and the next fetch runs, a previously-flagged tip's coverage
completes and it stops appearing on its own -- no code change needed in
`unresolved_tips()` itself, since it only ever measured coverage.

Seeded 2026-09-30 with the five researched cases: GMGI.US->MRDN.US (1.0),
FDP.US->DMC.US (1.0), VSCO.US->VSXY.US (1.0), FLGC.US->ZSTK.US (1.0),
LBRDA.US->CHTR.US (0.236) -- see doc/DELISTING_RESEARCH_2026-09-30.md for
the full research and the price-continuity verification for each.

### weekday/Saturday/Sunday split (2026-09-30)
**Decision:** `daily_update.py` takes a required `--mode
{tips-only,daily-price,intraday-price}` flag instead of always fetching
daily price data. Three separate systemd timer/service pairs in `ops/`
(`eodhd-daily-update.timer`, `-1d.timer`, `-5m.timer`) each fire with a
native systemd day-of-week filter (`OnCalendar=Mon,Tue,Wed,Thu,Fri ...`,
`OnCalendar=Sat ...`, `OnCalendar=Sun ...`) and pass a fixed `--mode`
through `ops/run_daily_update.sh`:
- Weekdays: `tips-only` -- parse new tip emails, no price fetch at all.
- Saturday: `daily-price` -- also fetch daily OHLCV into `PRICE_TABLENAME`.
- Sunday: `intraday-price` -- also fetch 5-minute OHLCV into
  `INTRADAY_TABLENAME` ("intraday_5m").

**Reasoning:** price fetching is the EODHD-call-expensive, error-prone part
of the pipeline (see "tips() per-tip failure handling" above); tip-email
capture is cheap and needs to happen daily regardless, since a missed day's
email is gone. Splitting them means weekdays stay fast and low-risk, and the
two price-fetch modes -- which are the more expensive and more failure-prone
operations -- each get a dedicated day rather than competing for the same
run's EODHD quota. Saturday for daily bars (markets closed, the whole
week's bars are published and settled) and Sunday for 5-minute intraday
(kept on a separate day from Saturday's fetch, and 5-minute bars generate
far more rows per ticker than daily bars, so it's also the more disk-hungry
of the two) were chosen over, e.g., both running the same day, purely to
spread load.

**Why systemd day-of-week filtering instead of in-script branching:** the
alternative (one timer firing daily, `daily_update.py` itself checking
`date.today().weekday()`) would push scheduling policy into application
code -- inconsistent with this project's existing separation of policy
(caller/ops decides *when* and *what*) from mechanism (the script/library
just does what it's told). It would also make manual testing harder: with
separate units, any mode can be run on demand (`sudo systemctl start
eodhd-daily-update-5m.service`) regardless of what day it actually is,
which mattered a lot during the debugging in "tips() per-tip failure
handling" above.

**Deferred:** a weekday fetch for a "priority" subset of tickers/tips (Ian
wants this eventually, not yet). `_log_unresolved_tips()` stays
daily-price-only for now (not extended to check `intraday_5m` coverage) --
once real 5-minute data exists, the plan is to audit it for consistency
against the daily table directly, which is a more direct check than routing
5-minute coverage through `unresolved_tips()` too.

### --all-emails for a second independent consumer of the same mailbox (2026-09-30)
**Problem:** Ian plans to run `daily_update.py` manually on his Mac, against
the same IMAP mailbox as `mars`'s scheduled runs, to build an independent
comparison database. `download_emails(..., unseen_only=True)` marks each
email `\Seen` on the mail server itself -- shared state, not per-machine.
Whichever of `mars`/the Mac happens to run first on a given day claims that
day's tip email; the other sees nothing new that day. Pointing the Mac's
`ops/daily-update.env` at a different `RCLONE_REMOTE` (separate OneDrive
path, already planned) solves "don't clobber mars's db" but not this --
the contention is upstream, at the mailbox, before either machine's
database is even involved.

**Decision:** a `--all-emails` flag on `daily_update.py`, forwarded through
`run_daily_update.sh`'s new passthrough (`run_daily_update.sh <mode>
[extra args]`, everything after `<mode>` passed straight to
`daily_update.py`). When set, it sets **both** `unseen_only=False` (search
everything, not just mail unseen by this machine) **and** the new
`mark_seen=False` (never flip `\Seen` on anything it downloads).

**Correction during implementation:** the first cut of this only set
`unseen_only=False` and missed that `download_emails()` marks `\Seen`
*unconditionally* on every downloaded email, regardless of `unseen_only` --
that flag only controls what gets searched for, not what gets marked
afterward. A Mac running only `unseen_only=False` would still steal
genuinely-new mail out from under `mars`: if the Mac happened to run first
on a given day, it would download and mark Seen a tip email `mars` hadn't
processed yet, and `mars`'s own `unseen_only=True` search would then find
nothing -- permanently, since (unlike the price-data backfill) there's no
mechanism to recover a tip that was never parsed into `tip_details` in the
first place. `mark_seen` (new, independent parameter on
`download_emails()`, default `True`) closes this: a secondary consumer
needs to never touch the shared Seen state at all, not just search past it.

**Reasoning:** re-parsing the full matching history every run is slower
(re-downloads emails already seen) but `tips_exchange2sqlite`'s `INSERT OR
REPLACE` makes it harmless/idempotent, and it was the simplest fix that
didn't require a second mailbox/forwarding setup. `mars`'s own scheduled
runs are unaffected -- the flag defaults off, so `unseen_only=True` and
`mark_seen=True` there, unchanged.

---

## tips_io design

### All tickers appended with ".US"
**Decision:** tips_io.py always appends ".US" to ticker codes.

**Reasoning:** StockDataAnalytics only covers US stocks. There is no exchange
indicator in the email HTML.

**Risk:** If the newsletter ever adds non-US stocks, this will silently produce
wrong codes. No defensive check exists for this.

### Two-format parser
**Decision:** `_parse_tip_card()` detects full-detail vs compact card format
per card (`tip_n <= 3` vs `tip_n > 3`) and branches internally, rather than
using two separate functions.

**Reasoning:** The email format changed between April 2026 (all full) and June
2026 (3 full + 17 compact). The two branches share enough structure (both
populate the same result dict) that a single function with an if/else reads
more clearly than two functions with a large overlapping field list.

**Risk:** Future email format changes will require updating the parser. The
format is controlled by the newsletter provider and can change without notice.
The test suite includes specific value assertions against real emails which
will catch format changes.

### Unified per-quality score column (supersedes "numeric score values absent
### from compact cards")
**Decision (2026-09-12):** Each of the four per-tip qualities (pattern
quality, setup, risk/reward, context) has exactly one score column
(`pattern_quality_score`, `setup_score`, `risk_reward_score`,
`context_score`) and one colour column, populated for *every* tip 1-20 —
not separate `..._number`/`..._height` columns with one half always zero.

**Reasoning:** The original decision (below, kept for history) held that
compact cards (tip_n 4-20) carry no numeric score, only a coloured bar, so
the number columns were left `None` for them. Forensic analysis of every
captured email (166 emails, 13,040 individual score bars) showed this bar
is not just a colour indicator — its rendering is fully deterministic from
the underlying score:
- Each quality has a fixed maximum: pattern_quality=40, setup=20,
  risk_reward=18, context=20 (these sum to 98, matching the newsletter's
  own "Total Score: X / 98" on every full card with zero exceptions).
- The bar's outer box is a constant 40px on full cards and 24px on compact
  cards, regardless of category, and the filled height is always exactly
  `score / category_max * box_height` (floor-rounded) — confirmed as a
  unique exact fit for Setup and Risk/Reward, and consistent for Pattern
  Quality and Context.
- The colour itself follows a single universal rule across all four
  categories and both card formats: fill fraction >=0.75 -> green, 0.50-0.74
  -> yellow, <0.50 -> orange (red never observed on any of the four
  qualities in the captured corpus). Zero exceptions across all 13,040 bars.

This means a compact card's bar height can be inverted back into an
estimated score using the same box/max constants (see `CATEGORY_MAX`,
`BOX_HEIGHT_FULL`, `BOX_HEIGHT_COMPACT`, `_height_to_score()` in
`tips_io.py`), giving one continuous score column across all 20 tips
instead of a column that's only ever populated for 3 of them.

**Precision caveat:** full-card scores are exact (the newsletter's own
printed number). Compact-card scores are *estimates* reconstructed from a
24px-tall bar and are only accurate to roughly +/-0.75-1.7 points depending
on category (see `doc/LIMITATIONS.md`). Downstream code that needs to tell
the two apart should compare `tip_n <= 3` rather than assume the column
means the same precision everywhere.

**Validation, not enforcement:** the parser cross-checks its own work on
every card — score bounds (`[0, category_max]`), the derived-vs-parsed
colour (should agree; see the original per-quality decision below on why
this is a stronger check on full cards than compact ones), and the full
card's own "Total Score: X / Y" text against the 4 parsed scores and the
expected /98 denominator — logging a warning (not raising) if the
newsletter's template or scoring rubric ever changes underneath us. As of
this decision, running the new parser across the entire 166-email archive
produces zero warnings.

**Original decision (2026-09 and earlier, now superseded above):**
pattern_quality_number, setup_number, risk_reward_number, context_number
were None for compact cards; only bar colours were extracted, because the
compact card format was believed not to encode a numeric score at all.

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
