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

**Validation** (on data from before the period only, so a later collapse can't remove a stock): a drawn stock-period is replaced by the next stock in its band's seeded order if its bars trade on fewer than 90% of SPY's sessions in the 5 years before the period (EODHD blending another company's bars into the code), if it never trades on a 26 December that was a US session (a foreign listing under a US code), or if it's on a short hand-checked list of other bad price series. That list now includes codes whose `ac` jumps more than 3x either way in one day before the period with no event behind it (in-sample series scanned 2026-11-23: a splice of two companies, a fortnight of junk bars, a change of price basis). Real moves of that size (meme spikes, trial results) were checked and kept. `draw_rejects` lists the in-sample replacements and why.

**Bands** (by that median turnover) and draw sizes: above $50m (40), $10m-50m (30), $1m-10m (30). Within each band and period, a seeded shuffle of the in-sample pool. **Inclusion probability** is the band's draw size over its in-sample pool size (`draw.inclusion_prob`), so results can be weighted to "the average stock" or "the average dollar".

## Tables

| Table | Key | Contents |
|---|---|---|
| `periods` | `period` | Start and end, and the span every stock-period in it gets. |
| `draw` | `period, code` | The 600 in-sample stock-periods: `band`, `draw_rank` (place in the band's seeded order), `inclusion_prob`, `eligible_in_pool` (in-sample pool size in the band), `seed`, `drawn_at`. |
| `draw_rejects` | `period, code` | In-sample stock-periods replaced by validation: `reason`, `replaced_by`. |
| `daily` | `code, timestamp` | Daily bars for the drawn stocks within their spans, plus SPY over all spans. `op hi lo cl` as traded, `ac` adjusted (as of the fetch, 2026-11-06 to 23: `fetch_status.fetched_at`), `vo`; rows with `vo = 0` are padding (prices carried forward). |
| `fetch_status` | `period, code` | Each fetch: `status` (`ok`/`error`), first and last bar with volume, `error`. SPY is period 0. |
| `series_gaps` | `period, code` | Stock-periods whose bars stop early or start late, or that EODHD had nothing for (`kind` below). Listed, never dropped. |
| `series_events` | `period, code` | What happened to each stopped series, researched from primary sources (see "Stopped series" below). |
| `follow_on_daily` | `code, date` | Daily bars of what a holder ended up in: an acquirer's shares, the same shares on another exchange, or an FX rate (`op hi lo cl ac vo`, as EODHD serves them, unpadded, on that market's own calendar). From 45 days before the stop to the span end. |
| `follow_on_status` | `code` | Each follow-on fetch: range, rows, time. |
| `event_checks` | `period, code, date` | The stock's last 5 closes beside what its deal terms imply that day (`implied`, `gap` = close / implied - 1). |
| `eligible` | `period, code` | Every eligible stock outside the hold-out pool, per period: `band`, `median_turnover`, `close`, `ot` (in that month's Alphanume Optionable Tickers snapshot), `venue_inferred`, `exchange` (current or last), `name`, `reused_symbol` (the code is in both symbol lists). |
| `pool_counts` | `period, band` | Eligible stocks per band, in each pool (the hold-out pool as a count only). |
| `screen_dropped` | `period, code` | Why other tickers trading on the screen date were left out (`reason`, `detail`); rows naming a hold-out stock are removed. |
| `extract_info` | `key` | Build time, eodhd2 commit, seed, rules; from builds after 2026-11-19, the source db's path, size, mtime, sha1 and origin (`jml_db_*`). |

### `series_gaps.kind`

| kind | meaning |
|---|---|
| `stops_in_period` | The last bar is before the period ends. |
| `stops_in_outcome_year` | The last bar is in the year after the period. |
| `starts_late` | The first bar is after the span starts (the 5-year screen used bulk data; the stock's own series disagrees). |
| `no_data` | EODHD's daily endpoint returned nothing. |

A series that stops is usually a takeover, a rename or a bankruptcy. Excluding these stocks would be survivorship bias, so each is researched instead.

### Stopped series (`series_events`)

Every `stops_in_period` / `stops_in_outcome_year` row has a `series_events` row (the build refuses otherwise), researched from SEC filings, the target's own wherever it filed one (`source`). The bars themselves aren't changed: `daily` still ends at the last trade, and the value after it comes from the event.

| column | meaning |
|---|---|
| `event` | `cash_takeover`, `cash_plus_cvr`, `stock_merger`, `cash_and_stock`, `cash_or_stock_election`, `foreign_delisting` |
| `announced`, `effective_date`, `last_trade_date` | Announcement, closing, and last US trading day per the sources (the last bar in `daily` can be a session earlier where EODHD lacks the final day; `notes` says so) |
| `cash_per_share` | Cash per share paid at closing, USD |
| `follow_code`, `follow_ratio`, `follow_currency` | Shares received per share held, and in what; their bars are in `follow_on_daily` |
| `fx_code` | An FX series in `follow_on_daily` (CHL only: `USDHKD.FOREX`, HKD per USD) |
| `cvr_terms`, `cvr_paid` | Contingent value rights: terms, and what has been paid so far |
| `recovery` | For a bankruptcy: what equity received (none in this build) |
| `notes` | Mechanics: two-step mergers, elections, missing final bars, how to value CHL |

A holder's value after the stop: `cash_per_share` + `follow_ratio` x the follow-on's price (converted with `fx_code` where given), plus any CVR, which is valued by jml (unpaid so far in this build). For an election (`cash_or_stock_election`), `follow_ratio` is the default a passive holder got; `notes` gives the alternatives.

Acquirers are named only where a holder received their shares; a cash deal's buyer isn't needed for the value and isn't recorded.

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

## This build: `jml_insample_2026-11-23.db`

Replaces `jml_insample_2026-11-08.db`: the stopped series are researched, and three bad series found while doing so are replaced. Built from `jml.2026-11-23b.db` (`extract_info.jml_db_*`), eodhd2 commit 3494dba.

**Size and coverage**
- 108 MB. 600 stock-periods, 527 distinct stocks (some drawn in two or more periods); 872,643 daily rows. EODHD returned data for every one.
- SPY: 2,909 sessions, 2014-10-20 to 2026-05-14.

**The sample**
- In-sample pools per band and period: 539-799 above $50m, 578-645 at $10m-50m, 613-703 at $1m-10m (`pool_counts`), so inclusion probabilities are 4-7%.
- 514 of the 600 are in OT (218 of 240, 152 of 180, 144 of 180 by band); 7 are `venue_inferred`.
- Validation replaced 10 in-sample stock-periods (`draw_rejects`): 2 blended codes (real bars on 28% and 88% of sessions), 2 foreign listings, 3 bad price series (one code in two periods, and a Russian listing), and, new in this build:
  - RLOG (period 3): two securities spliced. Rand Logistics to 2018-01-04 (last close $0.15; delisted, then Chapter 11 with nothing for shareholders), and an unidentified security from 2018-01-05 ($9.35 to $23). Replaced by GOL.
  - RRD (period 1): two weeks at about 1% of the price (2015-12-17 to 12-31), with `ac` on a different basis either side. Replaced by KAI.
  - CBD (period 3): `ac` triples on 2016-01-04 with no event behind it. Replaced by LDL.

**Changes from the 2026-11-08 build**
- EXAS's first fetch had stopped at 2025-11-19 (an EODHD gap, not an event). Refetched, it runs to its last trade, 2026-03-20.
- `ac` in the refetched or new series (EXAS, TAST, AVDL, GOL, KAI, LDL) is as of 2026-11-23; elsewhere it's as before.

**`series_gaps`: 21, all researched** (`series_events`; RLOG's has gone with it).
- `stops_in_period`: 3 (HBP, PDCE, SWI). `stops_in_outcome_year`: 18.
- Events: 11 cash takeovers, 2 cash plus CVR (AVDL, ETNB), 5 stock mergers (LPT, PDCE, PXD, HOUS, RDFN), 1 cash and stock (ONDK), 1 cash-or-stock election (SBOW), 1 foreign delisting (CHL). No bankruptcies.
- No CVR has paid anything yet (as of 2026-11-23).
- TAST and AVDL lack their final session in EODHD (still so on refetch); their last bar is the session before.

**`event_checks`:** the last close against the deal terms.
- Cash deals: within 0.1% of the cash price, except ONDK (cash and stock, 0.8% below).
- Stock deals: within 0.5% of ratio x the acquirer's close.
- AVDL closed 3.0% and ETNB 2.3% above their cash price: the market's value for their CVRs, which `implied` leaves out.
- CHL isn't in `event_checks` (its follow-on isn't in USD). Done by hand: its last five ADS closes were 7.8% below to 2.8% above 5 x 0941.HK / USDHKD (2.8% above on the last day), since Hong Kong closes about 14 hours before New York.
