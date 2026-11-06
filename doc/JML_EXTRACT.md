# jml in-sample extract: schema and caveats

For the jml analysis project (John MacLeod's per-stock models). Answers its data request (`jml/proposals/eodhd2-jml.md`). Built by eodhd2's `scripts/build_jml_extract.py` from `jml.db`, which `scripts/jml_screen.py` fills (the screen, the draw and the daily bars). The file is read-only; `extract_info` records the build.

## In-sample

- **The hold-out is by stock, not by date** (Ian, 2026-11-05). A seeded quarter of all eligible stocks, across every period, form the hold-out pool. None of them appears anywhere in this extract: not their bars, draw or eligible rows, nor a screen row naming one. Each period's in-sample draw comes from the other three quarters.
- **100 stocks per period, 6 periods:** 600 stock-periods. A stock can be drawn in more than one period; each is its own stock-period, with its own span.
- **Each stock-period's span** is 5 years before its period starts to 1 year after it ends, plus 14 calendar days either side (`periods.span_from` / `span_to`). A stock's bars are delivered only within its own spans.

| Period | Months | SPY |
|---|---|---|
| 1 | 2019-11 to 2019-12 | steady bull, low volatility |
| 2 | 2020-02 to 2020-03 | the COVID crash |
| 3 | 2020-04 to 2020-05 | the rebound |
| 4 | 2022-04 to 2022-06 | the 2022 bear market |
| 5 | 2023-08 to 2023-09 | a quiet, low-volatility decline |
| 6 | 2025-04 | the tariff shock |

## How the sample was drawn

At each period's **screen date** (the last session before it starts), from every US ticker in EODHD's bulk end-of-day data:

1. **Common stocks** by EODHD's symbol lists, current and delisted. These are excluded, though EODHD types them all as Common Stock:
   - closed-end funds, a hand-checked list;
   - SPAC units and shares, notes and trust certificates;
   - units, warrants, rights and preferreds, by code suffix (`-U`, `-UN`, `-W`, `-WS`, `-P-x`, `-CL`). Share classes (`BF-B`, `GOOG`/`GOOGL`) are kept.

   BDCs, royalty trusts, MLPs and REITs are kept.
2. **Venue:** listed on NYSE, NASDAQ, NYSE American, NYSE Arca or BATS. The lists give a stock's *current* exchange, so a company listed then and on the pink sheets now (SBNY, FRCB, BBBYQ, NVTAQ) looks like OTC. These are kept and flagged `venue_inferred` if the company is American. OTC ADRs and foreign ordinaries are dropped.
3. **Close of at least $2** on the screen date.
4. **Median turnover** (close x volume) of at least $1m over the 60 sessions up to the screen date. A session with no bar counts as zero.
5. **Traded on any of the 5 sessions up to 5 years before the period start.** That's knowable at the time; it does make the population "stocks listed at least 5 years".
6. **One stock, one code:** a rename EODHD lists under both symbols (BALL and BLL, ANTM and ELV) is kept once, under its current symbol. The test is closes within 0.1% on at least 5/6 of the 60 sessions. The ten rename pairs checked matched on all 60, against at most 25% for share classes.

Six common stocks have bulk bars but are in neither symbol list, nor in EODHD's search (LUNA and TBPH among them). They were added by hand. One code's bad prices (HLAN, about $19,800 a share) put it out.

**Bands** (by that median turnover) and draw sizes: above $50m (40), $10m-50m (30), $1m-10m (30). Within each band and period, a seeded shuffle of the in-sample pool. **Inclusion probability** is the band's draw size over its in-sample pool size (`draw.inclusion_prob`), so results can be weighted to "the average stock" or "the average dollar".

## Tables

| Table | Key | Contents |
|---|---|---|
| `periods` | `period` | Start and end, and the span every stock-period in it gets. |
| `draw` | `period, code` | The 600 in-sample stock-periods: `band`, `draw_rank`, `inclusion_prob`, `eligible_in_pool` (in-sample pool size in the band), `seed`, `drawn_at`. |
| `daily` | `code, timestamp` | Daily bars for the drawn stocks within their spans, plus SPY over all spans. `op hi lo cl` as traded, `ac` adjusted (as of the fetch, 2026-11-06), `vo`; rows with `vo = 0` are padding (prices carried forward). |
| `fetch_status` | `period, code` | Each fetch: `status` (`ok`/`error`), first and last bar with volume, `error`. SPY is period 0. |
| `series_gaps` | `period, code` | Stock-periods whose bars stop early or start late, or that EODHD had nothing for (`kind` below). Listed, never dropped. |
| `eligible` | `period, code` | Every eligible stock outside the hold-out pool, per period: `band`, `median_turnover`, `close`, `ot` (in that month's Alphanume Optionable Tickers snapshot), `venue_inferred`, `exchange` (current or last), `name`, `reused_symbol` (the code is in both symbol lists). |
| `pool_counts` | `period, band` | Eligible stocks per band, in each pool (the hold-out pool as a count only). |
| `screen_dropped` | `period, code` | Why other tickers trading on the screen date were left out (`reason`, `detail`); rows naming a hold-out stock are removed. |
| `extract_info` | `key` | Build time, eodhd2 commit, seed, rules. |

### `series_gaps.kind`

| kind | meaning |
|---|---|
| `stops_in_period` | The last bar is before the period ends. |
| `stops_in_outcome_year` | The last bar is in the year after the period. |
| `starts_late` | The first bar is after the span starts (the 5-year screen used bulk data; the stock's own series disagrees). |
| `no_data` | EODHD's daily endpoint returned nothing. |

A series that stops is usually a takeover, a rename or a bankruptcy. **These haven't been researched yet:** no aliases, deal prices or bankruptcy values are applied, so a stopped series simply ends. Excluding these stocks would be survivorship bias. Treat each as its own case, and ask eodhd2 for the research (as done for the tips, `TICKER_ALIASES.md`) before relying on returns that cross the stop.

## Caveats

- **John MacLeod traded ASX stocks; these are US stocks.** His findings are hypotheses to test here, not settings to copy.
- **Old companies under reused symbols are missing.** EODHD drops a company's history when its symbol is reused, so such a company is invisible to the screen. Alphanume's OT snapshots list some of these: 118 OT tickers were missing from the bulk data at period 1's screen date, falling to 14 at period 6's. Many of those are renames that appear under their new symbol, so the true gap is smaller.
- **Exchange is current, not as of the screen date** (see Venue above).
- **Turnover** is close x volume on unadjusted prices; EODHD has no VWAP.
- **`ac` is adjusted as of the fetch:** its level includes later splits and dividends, but ratios between dates are right. Use `ac` for returns.
- **OT** comes from Alphanume's monthly snapshots, whose point-in-time status is unverified. It covers 79-89% of eligible stocks, depending on the band.
- **Codes:** EODHD's, as of the fetch. Some odd ones are deliberate:
  - `HTZ-R-W` holds the old Hertz's bars, through its 2020 bankruptcy; it isn't Hertz's rights.
  - `_old` codes (e.g. `EV_old2`, the old Eaton Vance) are EODHD's names for an earlier company under a reused symbol.

## This build

(Filled in after the build.)
