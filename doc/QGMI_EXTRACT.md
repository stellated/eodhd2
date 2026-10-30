# qgmi in-sample extract: schema and caveats

For the qgmi analysis project. Answers its data request
(`qgmi/proposals/eodhd2-qgmi.md`). It's built by eodhd2's
`scripts/build_qgmi_extract.py` from `alphanume.db`, `universe.db` (EODHD
daily bars, `scripts/get_universe_daily.py`) and `sda.db`'s `ig_margins`.
The file is read-only; `extract_info` records the build.

## Timing

- QGMI publishes its basket at **16:05 ET on `date`** (the rebalance date,
  the first trading day of the month). It's first tradeable at the **next
  session's open**, and held until the open after the next rebalance.
- **The snapshot for a month** is the Optionable Tickers snapshot dated on
  that month's rebalance date. Every rebalance date has one, and every
  snapshot is on a rebalance date.
- **Data a replication may use for a month:** bars up to and including the
  rebalance day's close, and Alphanume rows dated on or before it.
- **In-sample:** 70 rebalances, 2019-01-02 to 2024-10-01.
  - No Alphanume row (picks, snapshot or market cap) is dated after 2024-10-01.
  - Daily bars run through 2024-11-04, the open after the 2024-11-01
    rebalance, which closes the last in-sample month.
  - The hold-out (from 2024-11-01) comes later, as a separate extract.

## Tables

All `code`s are EODHD's: Alphanume's ticker with `.` replaced by `-`, plus
`.US` (`BRK.B` -> `BRK-B.US`).

| Table | Key | Contents |
|---|---|---|
| `qgmi_picks` | `date, rank` | QGMI's 10 a month: `date` (rebalance), `ticker`, `code`, `rank` 1-10. |
| `optionable` | `date, ticker` | Optionable Tickers snapshots: `has_weeklies` (0/1), `avg_days_between` (Alphanume's average gap between listed expirations, days). |
| `daily` | `code, timestamp` | EODHD daily bars for every ticker in `optionable` or `qgmi_picks`, plus SPY and the 11 SPDR sector ETFs. Columns are eodhd2's daily contract: `date`, `op hi lo cl` as traded, `ac` adjusted, `vo`. Rows with `vo = 0` are padding (prices carried forward). Each ticker starts 400 days before its first snapshot (13 months' lookback); ETFs and SPY start 2017-12-01. |
| `fetch_status` | `code` | One row per ticker fetched: `status` (`ok`/`error`), `error`, and `first_date`/`last_date`, its first and last bars with volume (to 2026-10-29, the fetch date, not the extract's cut-off). |
| `ticker_aliases` | `old_code, effective_date` | Renames eodhd2 has researched: the old symbol's series continues under `new_code` x `ratio` from `effective_date` (or at a fixed `cash_price`). Researched for the newsletter's tips, not this universe, so most renames here have no alias. |
| `market_cap` | `rebalance_date, ticker` | Alphanume Historical Market Cap: the latest row on or before each rebalance date (`date` says which), for that month's universe and picks. |
| `ticker_classification` | `ticker` | Alphanume's `alphanume_sector`/`alphanume_industry`, and `sector_etf` from `sector_etf_map`. |
| `sector_etf_map` | `alphanume_sector` | Alphanume sector -> SPDR ETF. |
| `ig_margins` | `rebalance_date, code` | IG's row for each universe ticker or pick, from the latest IG file generated on or before the rebalance date. `age_days` = rebalance date minus the file's `source_modified`. `ig_code`/`renamed_on` as in `ig_margins_resolved`. |
| `bar_gaps` | `rebalance_date, code` | Universe tickers and picks with no usable bar on a rebalance date, and why (below). |
| `universe_counts` | `date` | Per snapshot: tickers, `has_weeklies`, those with a bar, those with $50m notional (both the weekly-options subset and the whole snapshot). |
| `extract_info` | `key` | Build time, eodhd2 commit, cut-offs, IG snapshots used. |

### `bar_gaps.reason`

| reason | meaning |
|---|---|
| `no_bars` | EODHD returned nothing for the symbol (`fetch_status.error`), or no bar with volume. Mostly delisted symbols EODHD doesn't serve. |
| `before_first_bar` | EODHD's series starts after this rebalance. Often a **reused ticker**: EODHD serves today's company under the symbol, not the one that was in the universe (AAC: in the universe from 2019, EODHD's AAC.US is a SPAC from 2021-03). |
| `after_last_bar` | EODHD's series ended before this rebalance (delisted or renamed without an alias), although Alphanume still lists it. |
| `missing_day` | Bars on both sides, none on the rebalance date. |
| `zero_volume` | The rebalance date's bar is padding. |
| `price_mismatch` | A bar exists, but its close is outside 0.67-1.5x Alphanume's implied price (`market_cap / shares_outstanding`, same date). Likely a reused ticker, or a split the share count hasn't caught up with; `detail` gives the ratio. The bar is kept; whether to trust it is the analysis's call. |

A gap is listed, never dropped. Tickers that later collapse are the ones
most likely to go missing, so excluding them is survivorship bias.

### Sector map

Alphanume's sectors aren't GICS, so the map is approximate:

| Alphanume sector | ETF |
|---|---|
| communications_media | XLC |
| consumer_cyclical | XLY |
| energy_resources | XLE |
| essential_goods | XLP |
| finance | XLF |
| healthcare | XLV |
| industrial_transport | XLI |
| raw_materials | XLB |
| real_assets | XLRE |
| technology | XLK |
| utilities_infrastructure | XLU |

Known mismatches:
- GICS puts GOOGL and META in XLC, and AMZN in XLY; Alphanume may not.
- `business_services` (in industrial_transport) includes IT services that
  GICS puts in XLK.
- The GICS reshuffles of 2018 and 2023 moved stocks between ETFs.
- Tickers with no classification have `sector_etf` NULL.

## Caveats

- **`ac` is adjusted as of the fetch (2026-10-29).** Its level includes
  later splits and dividends, but the ratio between two dates doesn't
  change, so it's right for returns. For stop levels on as-traded prices,
  scale `op`/`lo` by `ac / cl`.
- **Notional volume** is (`hi` + `lo` + `cl`) / 3 x `vo` on unadjusted
  prices. EODHD has no VWAP.
- **`has_weeklies`** is Alphanume's flag, and its definition is
  undocumented. It may not match the article's "at least 6 consecutive
  weekly expirations".
- **Point-in-time is unverified** for every Alphanume dataset here:
  - QGMI picks: Alphanume claims point-in-time, never revised.
  - Optionable Tickers and market cap: unknown.
  - No live capture was made before access ended to test either.
- **Ticker Classification is current (2026-10),** not point-in-time, and
  covers only tickers that still exist. A stock that changed business
  carries its 2026 sector. Most delisted tickers have none, so they drop
  out of the sector question.
- **Market cap** may be missing for some tickers, or stale (check
  `market_cap.date` against `rebalance_date`).
- **IG files are sparse snapshots:** 2022-01-17, then 2023-03 to 2024-04,
  with nothing before 2022. For most rebalances the nearest file is
  months old (`age_days`), so actual cover is approximate at best.
- **Renames:** EODHD stops serving a ticker under its old symbol after a
  rename. `ticker_aliases` covers only renames researched for the
  newsletter's tips, so a renamed universe ticker usually shows as
  `after_last_bar` from its rename.
- **6 QGMI tickers were never in Optionable Tickers;** they're in `daily`
  and `bar_gaps` all the same.

## Checks run by the build

- Exactly 10 picks a month, ranks 1-10.
- A snapshot exists on every rebalance date.
- No `daily` row after 2024-11-04; no Alphanume row after 2024-10-01.

Each failure stops the build. The results for this build are below.

## This build: `qgmi_insample_2026-10-29.db` (eodhd2 3beaf8d)

**Size and coverage**
- 1.07 GB. 70 rebalances, 700 picks, 257,760 snapshot rows, 6.87M daily rows.
- 5,601 codes. 29 of them got nothing from EODHD (`fetch_status.status = 'error'`).
- 4 picks are not in that month's snapshot: VIPS, SE, EDU and TAL. All are
  ADRs; their bars are present.

**Universe size per snapshot (min / median / max):**

| | min | median | max |
|---|---|---|---|
| tickers | 3,241 | 3,756 | 4,130 |
| `has_weeklies` | 439 | 484 | 551 |
| ... with a bar on the rebalance date | 426 | 478 | 541 |
| ... and $50m notional | 321 | 364 | 404 |

That last row is against the article's "less than 800, then ~350". For the
whole snapshot, $50m notional alone gives 579 / 925 / 1,144.

**`bar_gaps`:** 7,712 rows. Most are tickers without `has_weeklies`; 693
have it.

| reason | rows | tickers | of which picks |
|---|---|---|---|
| `before_first_bar` | 4,318 | 178 | 2 |
| `price_mismatch` | 1,996 | 80 | 1 |
| `no_bars` | 1,041 | 29 | 0 |
| `zero_volume` | 291 | 29 | 0 |
| `after_last_bar` | 66 | 4 | 0 |

- **Picks:** only SPWR has gaps.
  - 2019-10-01 and 2021-03-01: EODHD's SPWR.US starts 2021-04-19.
  - 2021-05-03: price mismatch, 0.40x.
- **`price_mismatch` comes in two kinds:**
  - **37 tickers have a constant ratio** in every month (e.g. CMRE 0.200,
    GLMD 0.125, DBVT 5.000). That's Alphanume's share count on another
    basis, such as adjusted for a later reverse split, or ADR ratios. The
    EODHD bars are fine.
  - **40 tickers have a ratio that varies.** These are mostly a different
    company under the same symbol, i.e. a reused ticker: B (Barnes; EODHD
    serves Barrick), DOC, FI, GEN, COHR, IR, RBC, WTW, CTRA, CZR, XPO,
    SAFE, AXON and others. Treat their bars as wrong for those months.
  - 3 more (ADXS, VYNE, SBEV) have a ratio of 0.

**Market cap**
- Missing for 27,531 of 257,760 snapshot rows (10.7%).
- Dated before the rebalance date for 19 rows.

**Ticker Classification:** covers 3,459 of the 5,585 universe tickers (62%).

**IG:** 33 rebalances, from 2022-02-01, have a file. `age_days` runs from
0 to 408.
