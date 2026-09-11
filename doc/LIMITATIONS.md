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