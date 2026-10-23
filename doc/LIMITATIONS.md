# Known Limitations

## 1. Pre-2006 Data Clipping
- The `XNYS` exchange calendar starts at **2006-06-30**. Any daily data before this date is clipped with a warning.
- **Workaround**: Use EODHD's `from_date` parameter to limit downloads to post-2006 data.

## 2. Half-Day Sessions
- `exchange_calendars` does not model half-day sessions (e.g., July 3rd before Independence Day).
- **Workaround**: The code uses calendar-day buffers for fetches, but padding logic skips half-days.

## 3. Email Format Fragility
- The parser is tightly coupled to StockDataAnalytics’ HTML structure.
- Format changes (e.g., April 2026 vs. June 2026) may require updates to `_parse_tip_card()`.
- Confirmed again 2026-08-31: the newsletter changed its template (see
  `doc/DESIGN_DECISIONS.md`, "newsletter template change 2026-08-31"),
  silently NULLing `holding_period_low`/`high` and `expected_reward`/
  `expected_risk` for every full-detail tip (tip_n 1-3) until caught by
  `update_outcomes()`'s per-position error logging. Fixed, but the
  underlying fragility is unchanged — a future template change would
  silently NULL fields again until something downstream notices.
- **Compact-card (tip_n 4-20) `expected_reward`/`expected_risk` are
  permanently NULL from 2026-08-31 onward** — not a parser gap, the
  newsletter stopped printing that breakdown for compact cards at all,
  replacing it with a single net `expected_return` figure (added as a
  new column) and an R:R ratio (not captured). Not recoverable by
  recomputing from entry/target/stop — confirmed these were the
  newsletter's own probability-weighted model output, not simple
  arithmetic, by checking a worked pre-change example.

## 4. `Gmtoffset` Assumption
- EODHD intraday CSVs assume `Gmtoffset=0` (UTC). If this changes, the code will log a warning.

## 5. Compact-card (tip_n 4-20) score precision
- Tips 1-3 ("full-detail" cards) show the newsletter's own printed number for
  each of the four qualities (pattern_quality_score, setup_score,
  risk_reward_score, context_score) — exact.
- Tips 4-20 ("compact" cards) show only a coloured bar, no number. The score
  is *estimated* by inverting the bar's filled pixel height against a fixed
  0-to-category-max scale (see `doc/DESIGN_DECISIONS.md`, "Unified per-quality
  score column"). Because the bar is only 24 pixels tall, this estimate has
  limited resolution: roughly +/-0.75 points for risk_reward/context/setup
  (max 18-20) and +/-1.7 points for pattern_quality (max 40).
- Downstream code that needs to distinguish exact from estimated values
  should key on `tip_n <= 3`, not on the score column alone (both cases
  populate the same column, by design — see `doc/DESIGN_DECISIONS.md`).
- The parser logs a warning if a computed score falls outside `[0,
  category_max]` or if the colour implied by a score disagrees with the
  colour parsed from the HTML — a signal (not a guarantee) that the
  newsletter's rendering has changed in a way that would invalidate this
  estimate.

## 6. IG margins: no point-in-time history before 2026-10-23
- IG keeps no past versions, so `ig_margins` has only the snapshots we
  could recover, with gaps of 21 months (2022-01 to 2023-08) and 18 months
  (2024-04 to 2025-10).
- Real snapshots (by `source_modified`): 2022-01-17, monthly 2023-08-21
  to 2024-04-10, 2025-10-06, then 2026-10-22 on (`source_modified` is
  when IG generated the file; `download_date` when we got it).
- So there's no IG data as of a 2026 tip's own date. The 2024 file is too old
  for recent listings; the current one omits stocks delisted since the
  tip (survivorship), and borrow rates and shortable flags change over
  time.
- `ig_margins_resolved` follows renames both ways, but only those in
  `ticker_aliases`, so coverage is understated for unresearched renames.

## 7. EODHD 5m volume disagrees with daily volume
- Prices in `intraday_5m` match `daily` (5m highs/lows equal the daily
  high/low), but volume often doesn't. Measured 2026-10-18 over 97,276
  ticker-days, as the sum of a day's regular-hours 5m volume over daily
  volume: median 0.85 (expected, since daily also counts extended hours and
  auctions), but 3,860 days are above 1.25x (up to 37x) and 2,157 below 0.5x.
- **Too high:** mostly whole dates where 10-38% of tickers are inflated at
  once: 2026-03-16..19, 04-30..05-11, 08-19..28, 10-13..15 (TSLA 05-08:
  368M in 5m bars vs 65M daily). EODHD's live API returns the same numbers,
  so re-fetching doesn't help.
- **Steady factor per ticker:** some tickers run at a constant ratio for
  weeks or months (COIA 2.00x until 2026-05-27 then 1.00x; IESC ~0.46x;
  CRWD ~0.22x; APH ~0.40x in July-August) with every bar trading, so the
  two feeds count volume differently rather than 5m data being missing.
- **Effect:** nothing in the pipeline uses 5m volume as a quantity -- only
  `vo > 0` ("did this bar trade"), which this doesn't affect. But
  `outcomes.buy_bar_volume`/`sell_bar_volume` inherit the problem; use
  `daily.vo` for any liquidity or slippage work (see
  `doc/OUTCOMES_SCHEMA.md`).
