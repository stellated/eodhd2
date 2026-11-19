# Alphanume data: what we captured and what we found

Alphanume (alphanume.com) sells event-driven and quant datasets on US
stocks. Ian took the 3-day Pro trial in October 2026, captured the full
history of every dataset, and decided not to subscribe. This note
collects what's known about each dataset in one place; the day-by-day
record is in `DAProjects/Alphanume Trial Plan.md`, and the capture design
in `doc/DESIGN_DECISIONS.md` ("Alphanume trial capture", "EODHD budget for
the alphanume datasets").

Written 2026-11-19 from those notes, the extract docs
(`QGMI_EXTRACT.md`, `SDA_EXTRACT.md`, `JML_EXTRACT.md`), the analysis
repos' READMEs, and a re-read of `alphanume.db`.

## The capture

- **Window:** trial started 2026-10-27; history pulls that day; live
  captures 10-27 to 10-28. Access ended at 23:38:55 ET on 2026-10-28,
  when Ian cancelled (not on 10-31, when the trial was due to end): every
  request after that returned HTTP 403 `PRO_SUBSCRIPTION_REQUIRED`.
- **Storage:** `~/data/alphanume.db` on mars (1.46 GB), copied to
  OneDrive at 23:44 ET on 10-28. Every response body is stored
  unchanged (`raw_responses`), with the request and fetch time; nothing
  is parsed in it. `windows.capture` is `history` or `live`. The extract
  builders parse what each analysis repo needs.
- **API:** JSON, `X-API-Key` header, 600 requests a minute. Every field
  name matched the docs and every history pull completed with no HTTP
  errors. Every dataset's date field is `date`.
- **Incomplete:** a per-ticker gap pull for the SDA tips was running at
  the cutoff. Market cap finished (all 2,292 tipped tickers); Filing
  Intensity reached 1,093 of 2,292 (about "KO" alphabetically); Wikipedia
  Views covers only QGMI's tickers. 296 refused requests are stored as
  errors.
- **Licence, unanswered:** Alphanume hasn't said whether data captured
  during a trial may be kept and used after cancelling. Their only
  answer ("move at your own pace and test out the data for as long as
  you need") doesn't address it.

## Point-in-time: what's been tested

Alphanume describes its history as point-in-time and never revised. The
only test is the live captures against the history pull, over about two
days:

- **NDM, Pre-Market Drop, Risk Regime:** no revisions. Every difference
  was an outcome field filling in from NULL when expected (NDM
  `return`/`absolute_move` after the trade day's close; Pre-Market Drop
  `intraday_return_pct` after the close and `return_lead_1d` a day
  later). `return_lead_5d`/`30d` fill in later than the trial lasted.
- **Dilution:** one live capture (2026-10-28 23:15 ET), 12 rows: 8 match
  the history pull field for field, 4 are filings made after it. So
  `dilutive` didn't change for those 8, which is weak evidence it isn't
  revised. The fields that are known to change (below) were not caught
  changing live.
- **Everything else** (QGMI, Optionable Tickers, Reverse Splits, Market
  Cap, Filing Intensity, Wikipedia Views): untested. The day-3 re-pull
  that would have compared more never ran.

**Rule for the extracts** (Ian, 2026-10-28): a field filled in after the
event carries the date it became available, and is used only from then;
a field whose available date can't be identified is withheld.

## Datasets

Counts are from the 2026-10-27 history pull (Trial Plan, "Day 1
results"). IG cover is from the 2026-10-28 IG file via
`ig_margins_resolved`; "open" = new positions allowed, "shortable" = also
shortable. The current IG file omits names delisted since, so the last
90 days is the fairer figure.

### Next-Day Movers (NDM)

- **What:** 5 stocks a day expected to move more than usual the next
  session, from options-implied volatility. Non-directional.
- **History:** 3,435 rows, 2024-01-31 to 2026-10-26, 166 tickers. All
  687 sessions present, 5 picks each; no duplicates.
- **Fields:** `date`, `ticker`, `return`, `absolute_move`.
- **`date` is the pick date,** published at 15:30 ET for the next
  session (confirmed by the live captures).
- **`return` is close-to-close** (pick date's close to the trade day's
  close), not open-to-close: against EODHD, median difference 0.07
  points close-to-close, 2.8 open-to-close (19 of 20 sampled). So it
  includes the overnight gap, which an ORB entered after the open
  doesn't trade. `absolute_move` is exactly `|return|`.
- **Outcome:** mean return +0.15%, 45% positive, median absolute move
  4.8%.
- **Tickers:** all known to EODHD. Liquid, optionable names.
- **IG:** listed 68% / 50% (all / last 90 days), open 56% / 38%,
  shortable 42% / 35%, median borrow 0.8%.
- **Verdict:** clean and complete. History is short (under 3 years) for
  a strategy test. Analysis repo: `ndm` (ORB).

### Pre-Market Drop Risk

- **What:** each morning, stocks likely to close at least 5% below their
  open, scored from pre-market behaviour. Ranked, with `prob_drop`.
- **History:** 6,408 rows, 2024-05-10 to 2026-10-27, 1,101 tickers. 1 to
  27 names a day (median about 10). No list on 2026-05-20 or 2026-09-04.
- **Fields:** `rank_for_date`, `prob_drop` (0.505 to 0.945),
  `px_at_trading`, `sub_dollar`, `premarket_return_at_cutoff`, `t_0` to
  `t_9`, `return_lag_1y`/`1m`/`5d`, `market_cap`, and the outcomes
  `intraday_return_pct`, `return_lead_1d`/`5d`/`30d`.
- **`date` is the trade day:** final by 09:05 ET in the live captures.
  Scores use prices up to 09:00.
- **Undocumented:** `t_0` to `t_9` look like a pre-market return path
  (SMX 2024-12-26 jumps from 0.69 to 74.22 at `t_6`). `market_cap` looks
  like thousands of dollars (SMX at $0.37 shows 54,920); confirm before
  use.
- **The ranking works, in order:** 53% of all names fell 5% or more open
  to close (mean -3.1%, median -6.1%); rank 1 65% (median -12.3%), ranks
  2-3 60%, 4-10 50%, 11+ 46%; `prob_drop` 0.8+ 70%, 0.5-0.6 48%.
- **`intraday_return_pct`** is EODHD's open-to-close exactly (19 of 20
  sampled; the 20th is a bad EODHD bar).
- **Tickers:** 17 unknown to EODHD (144 events). Many sub-dollar,
  illiquid names, mostly not in Optionable Tickers. Needs 1m data for
  the pre-market hours (5m is regular hours only).
- **IG:** listed 62% / 64%, open 1% / 0%, shortable 1% / 0%.
- **Verdict:** the clearest signal of any dataset, and untradeable at
  IG. Analysis repo: `pmd`.

### Quant Galore Momentum Index (QGMI)

- **What:** 10 momentum stocks a month, rebalanced on the first session
  (published 16:05 ET).
- **History:** 1,180 rows, 2017-01-03 to 2026-10-01, 212 tickers. Fields:
  `date`, `ticker`, `rank`.
- **Tickers:** all known to EODHD. 4 picks (VIPS, SE, EDU, TAL, all ADRs)
  aren't in that month's Optionable Tickers snapshot; 6 QGMI tickers
  never are.
- **IG:** listed 76% / 70% (all / since 2026-07), open 73% / 70%,
  shortable 72% / 62%, median borrow 0.8%.
- **Verdict:** the longest history of the strategy datasets, including
  2022's bear market, which the cherry-picking question needs.
  Point-in-time untested. Analysis repo: `qgmi`.

### Optionable Tickers

- **What:** monthly snapshot (first trading day) of tickers with listed
  options.
- **History:** 343,754 rows, 2019-01-02 to 2026-10-01, 6,063 tickers;
  3,241 to 4,130 a month (median 3,756).
- **Fields:** `date`, `ticker`, `has_weeklies` (0/1), `avg_days_between`
  (average gap between listed expirations, days).
- **`has_weeklies` is undocumented,** and may not match QGMI's "at least
  6 consecutive weekly expirations". 439 to 551 a month have it.
- **Reused tickers:** the snapshots list old companies under symbols
  EODHD now serves for someone else, so joins need care. At jml's period
  1 screen date (2019-10), 118 OT tickers were missing from EODHD's bulk
  data, falling to 14 by period 6 (2025-03); many are renames that
  appear under their new symbol.
- **Coverage:** 79-89% of jml's eligible stocks (>= $1m median
  turnover, >= $2), depending on the turnover band.
- **Verdict:** useful as a universe and a liquidity flag; point-in-time
  untested. Used by `qgmi` and `jml`.

### Reverse Splits

- **What:** reverse stock splits, with distress flags.
- **History:** 3,986 rows, 2022-10-03 to 2026-10-27, 3,069 tickers;
  about 3 a day (70-100 a month).
- **Fields:** `execution_date`, `first_seen_at`, `split_from`/`split_to`/
  `ratio`, `is_deficiency_candidate`, `sub_dollar_flag`,
  `market_cap_before`, `market_cap_tier`, `sector`, `optionable_flag`,
  `dilution_link_*`, `shelf_link_*`, `published_at`, `last_updated`.
- **Backfilled, not captured:** 3,813 rows were written in one batch on
  2026-08-29, and their `first_seen_at` is just midnight on the
  execution date. When those splits were announced isn't known, so the
  event date is the execution date. Rows captured live since then were
  first seen typically 0-12 days before execution.
- **The dilution links are withheld:** computed in the 2026-08-29 batch,
  they may count filings made after the split. `rs` rebuilds them from
  Dilution, using only earlier filings (possible only from 2025-01).
- **Mostly empty:** `market_cap_tier`, `optionable_flag` and `sector`;
  use the Market Cap and Optionable Tickers datasets instead.
  `market_cap_before` is unavailable before 2024.
- **Tickers:** about 1,500 rows are OTC (PINK, OTCQB, OTCQX); 156 tickers
  (168 events) are unknown to EODHD; 376 rows are ETFs, mostly leveraged
  and inverse, whose reverse splits are routine (reported separately).
- **IG:** listed 37% / 48%, open 3% / 1%, shortable 2% / 1%.
- **Verdict:** usable from the execution date only; close to untradeable
  at IG. Analysis repo: `rs`.

### Dilution (S-1 filings)

- **What:** SEC S-1 registration filings, flagged dilutive or resale.
- **History:** 926 rows, 2025-01-03 to 2026-10-26, 511 tickers; about 2 a
  day. 520 are `dilutive = 1`, on 284 days.
- **Fields:** `filing_timestamp`, `market_cap_at_filing`, `dilutive`,
  `resale`, `shares_offered`, `became_effective`, `effective_date`,
  `days_to_effective`, `offering_withdrawn`, `withdrawal_date`,
  `days_to_withdrawal`, `root_file_number`, `accession_number`,
  `filing_url`, `last_updated`.
- **Timing:** most filings are made 16:00-18:00 ET; rows appear in a
  nightly batch at about 23:00 ET, so the earliest entry is the next
  open.
- **Filled in later:** `became_effective`, `effective_date` and
  `offering_withdrawn` (896 of 926 rows have a `last_updated` after
  their filing date). Known on the filing night: `dilutive`, `resale`,
  `shares_offered`, `market_cap_at_filing`, the filing time.
- **Mostly microcaps:** only 48 of 508 dilutive filings with a market cap
  were above $300m (16 above $2bn). 6 tickers unknown to EODHD.
- **IG (dilutive filings):** listed 69% / 75%, open 5% / 5%, shortable
  3% / 2%.
- **Verdict:** short history (under 2 years); close to untradeable at
  IG. Analysis repo: `ds1`.

### S&P 500 Risk Regime

- **What:** a daily 0/1 label: 0 risk-on, 1 risk-off ("elevated
  volatility and stress"), from forward-looking implied volatility.
  Published 10:10 ET for that day.
- **History:** 3,916 rows, 2011-03-30 to 2026-10-27; 1 on 29% of days.
  No label on 2026-06-29 or 2026-08-26.
- **Against EODHD's VIX close:** mean 16.4 on 0 days, 22.3 on 1 days;
  every crisis day checked is 1 (2020-03-16 at VIX 82.7, 2024-08-05,
  2025-04-08); no single VIX level matches it more than 75% of the time.
- **Behaviour:** 93 switches in 15.6 years (runs of about 6 weeks). A
  stress flag, not a bear-market flag: 1 on 47% of 2022's days, 41% of
  2025's, 6% of 2017's.
- **Timing:** a pre-open trade can only use the previous session's
  label.
- **Verdict:** long, clean history. Used in `sda` (with `available_at`)
  and listed for `qgmi`.

### Historical Market Cap

- **What:** daily market cap and shares outstanding per ticker, in
  dollars.
- **History:** 2015-09-30 to 2026-10-26, pulled per ticker for the
  strategy datasets' tickers and the SDA tips. 1,843 of the first 3,834
  tickers returned nothing, nearly all Reverse Splits' OTC and
  long-delisted names; the strategy datasets' tickers are at most 3%
  empty.
- **Share-count basis:** in QGMI's extract, Alphanume's implied price
  (`market_cap / shares_outstanding`) is outside 0.67-1.5x EODHD's close
  for 80 tickers. 37 have a constant ratio (e.g. CMRE 0.200, DBVT 5.000):
  shares on another basis, such as adjusted for a later reverse split,
  or ADR ratios. 40 vary, mostly a reused ticker where EODHD serves a
  different company (B, DOC, FI, GEN, COHR...).
- **Gaps:** missing for 10.7% of QGMI's snapshot rows.
- **Verdict:** good cover for listed names; check the share basis before
  using shares outstanding. Publication time unknown, so `sda` takes a
  row as available two sessions after its date.

### Filing Intensity

- **What:** SEC filings per ticker per day (`filing_count`), refreshed
  nightly at 23:30.
- **History:** 2020-01-02 to 2026-10-26. 3.59M rows in all, 87% of them
  zero. 2,333 of the first 3,834 tickers returned nothing.
- **Incomplete:** the SDA gap pull stopped at 1,093 of 2,292 tipped
  tickers.
- **Verdict:** plain counts, with no filing type. Same availability rule
  as market cap in `sda`.

### Wikipedia Views

- **What:** daily page views per ticker, with a 30-day average and
  z-score.
- **History:** 138,783 rows, 2024-01-02 to 2026-10-26, 142 tickers; 70 of
  QGMI's 212 tickers return nothing.
- **Verdict:** sparse; pulled for QGMI's tickers only, so `sda` has it
  for 109 codes.

### Ticker Classification

- **What:** `alphanume_sector` and `alphanume_industry`.
- **History:** none: one current snapshot (2026-10), 5,634 tickers.
- **Not GICS:** `qgmi` maps sectors to SPDR ETFs approximately (GOOGL,
  META and AMZN may not land where GICS puts them).
- **Coverage:** tickers that still exist only (62% of QGMI's universe),
  and a stock that changed business carries its 2026 sector.
- **Verdict:** not point-in-time; use with care.

## Overall

- **Quality:** good. No duplicates, few gaps, field names as documented,
  and the outcome fields agree with EODHD. The weak spots are
  documentation (`has_weeklies`, `t_0`..`t_9`, units of Pre-Market
  Drop's `market_cap`, share-count basis) and Reverse Splits' backfill.
- **Depth:** most event datasets start in 2024-2025 (as Alphanume said:
  "around 2024"). QGMI, Optionable Tickers, Risk Regime and Market Cap go
  back further.
- **Point-in-time:** tested for two days on three datasets, no
  revisions; untested elsewhere.
- **Tradeability at IG:** fine for NDM and QGMI; Pre-Market Drop,
  Dilution and Reverse Splits are close to unshortable (about 0 names a
  week).
- **Decision (Ian, 2026-10-28): no paid subscription.** The history is
  captured; a subscription would add only live lists, and nothing has
  yet been shown worth trading live. Still open: keeping the data
  (licence) and whether Pro is now $99 a month, which would make
  subscribing later cheap.

## EODHD needs

From `scripts/eodhd_budget.py` on the real pulls: 3,834 tickers, 18,848
calls with merged spans (56,118 at one request per event day; the limit
is 100,000 a day), about 0.9 GB, mostly Pre-Market Drop's 1m days. 179
tickers aren't in EODHD's symbol lists (156 Reverse Splits, 17
Pre-Market Drop, 6 Dilution). Where 1m data stands in for 5m, it needs a
spike filter on highs and lows ("EODHD 1m bars aggregated to 5m" in
`doc/DESIGN_DECISIONS.md`).

## Where each dataset went

| Analysis repo | Alphanume datasets | Extract |
|---|---|---|
| `qgmi` | QGMI, Optionable Tickers, Market Cap, Ticker Classification | `QGMI_EXTRACT.md` (delivered) |
| `sda` | Risk Regime, Market Cap, Filing Intensity, Wikipedia Views | `SDA_EXTRACT.md` (delivered) |
| `jml` | Optionable Tickers (the `ot` flag) | `JML_EXTRACT.md` (delivered) |
| `ndm` | Next-Day Movers | proposal only |
| `pmd` | Pre-Market Drop Risk | proposal only |
| `rs` | Reverse Splits, Dilution, Market Cap, Optionable Tickers | proposal only |
| `ds1` | Dilution, Market Cap, Filing Intensity | proposal only |
