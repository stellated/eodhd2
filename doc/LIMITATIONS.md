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