# sda in-sample extract: schema and caveats

For the sda analysis project. Answers its data request
(`sda/proposals/eodhd2-sda.md`). It's built by eodhd2's
`scripts/build_sda_extract.py` from a copy of `sda.db`, `alphanume.db`
and a few EODHD calls (SPY). The file is read-only; `extract_info`
records the build. Delivered with it: `OUTCOMES_SCHEMA.md` (the rules of
the baseline trade, every `outcomes` column) and `TICKER_ALIASES.md`
(renames, mergers and cash-outs, cut to the in-sample tips).

## In-sample

- **Tips dated 2026-04-08 to 2026-09-01:** 103 email dates, 4,059 tips,
  1,830 tickers. Tips from 2026-09-02 are the hold-out (Ian,
  `DAProjects/SDA Tips Database 2026.md`) and nothing about them is in the
  extract: no `tip_exchange`, `tip_details` or `outcomes` row, no alias or
  doc line that names one.
- **Price bars** cover each tip's own window, 20 sessions before the tip
  date to 20 after (as `tips()` fetched them), so they run from 2026-03-10
  to 2026-09-30. Use them only for in-sample tips.
- **The tip date is the entry day.** The email arrives a few hours before
  the US open; day 0 of the entry window is the tip date itself.

## Tables

All `code`s are EODHD's (`AAPL.US`); tickers renamed since are under the
symbol the newsletter tipped (see `ticker_aliases`).

| Table | Key | Contents |
|---|---|---|
| `tip_exchange` | `exchange, tip_date` | Each email's market header: state, week/month change and colour, volatility, regime score and colour. |
| `tip_details` | `exchange, tip_date, tip_n` | The tips: entry zone, target, stop, hold, win probability, expected reward/risk/return, the four scores and colours. As in eodhd2's `doc/TECHNICAL_SPEC.md` (tips_io). |
| `outcomes` | `code, tip_date` | The baseline trade, one row per code and date (the lowest `tip_n` when a ticker is tipped twice in a day). Rules and columns in `OUTCOMES_SCHEMA.md`. |
| `daily` | `code, timestamp` | Daily bars for each tip's window, plus SPY. `op hi lo cl` as traded, `ac` adjusted (as of the fetch), `vo`; `vo = 0` rows are padding. |
| `intraday_5m` | `code, timestamp` | 5m bars, regular hours, same windows, plus SPY. `local_date`/`local_time` are New York time. |
| `tip_windows` | `code, tip_date` | Each tip's window: `start_date`, and the last date in `daily` (`daily_end`) and `intraday_5m` (`intraday_end`). |
| `ticker_aliases` | `old_code, effective_date` | The aliases in-sample tips reach: the old symbol continues as `new_code` x `ratio`, or at `cash_price`, from `effective_date`. Already applied to the bars. |
| `intraday_unavailable` | `code, tip_date, tablename` | Tips EODHD confirmed it has no 5m data for; their outcomes are from daily bars (`fill_granularity = '1d'`). |
| `risk_regime` | `date` | Alphanume's S&P 500 Risk Regime (0 risk-on, 1 risk-off), 2025-03 to 2026-09-01, with `available_at`. |
| `market_cap` | `code, date` | Alphanume Historical Market Cap and shares outstanding, with `available_date`. |
| `filing_intensity` | `code, date` | Alphanume SEC filings a day (`filing_count`), with `available_date`. |
| `wikipedia_views` | `code, date` | Alphanume Wikipedia page views, 30-day average and z-score, with `available_date`. |
| `ig_margins` | `download_date, code` | IG's offering for tipped codes: `new_positions`, `shortable`, borrow rate, tier-1 limit and rate. `ig_code`/`renamed_on` as in eodhd2's `ig_margins_resolved`. `source_modified` is when IG generated the file. |
| `window_gaps` | `tablename, code, tip_date` | Tips with fewer than 20 sessions of `daily` or `intraday_5m` after the tip date. |
| `outcome_gaps` | `code, tip_date` | Tips without a finished outcome (none in this build). |
| `extract_info` | `key` | Build time, eodhd2 commit, cut-offs, the assumptions below. |

## When data became known

Data a tip may use is data available before the open on its tip date.

- **`risk_regime`:** published at 10:10 ET on its own `date`
  (`available_at`), so a pre-open tip can only use an earlier date's label.
- **`market_cap`, `filing_intensity`, `wikipedia_views`:** Alphanume
  doesn't say when it publishes. Its datasets appear in a nightly batch
  (Dilution's at about 23:00 ET), so a row dated D is taken as available
  from the open **two sessions after D** (`available_date`), a day's margin
  beyond that. Join on `available_date <= tip_date`, not on `date`. Rows
  not available by 2026-09-01 are left out.
- **`ig_margins`:** IG keeps no past versions, and none of ours is from
  the in-sample period. The files are 2025-10-06 (six months before the
  first tip) and from 2026-10-22 (after the hold-out began). They're the
  best available view of whether IG offers a stock, not what it offered on
  the tip date. The later files also miss stocks delisted since
  (survivorship).
- **Point-in-time is unverified** for the Alphanume series: they're from
  history pulls, not captured live, so later revisions can't be ruled out.

## Caveats

- **The 2026-08-31 template change.** From that date, compact cards
  (`tip_n` 4-20) have no `expected_reward`/`expected_risk`, and
  `expected_return` appears. In-sample has two such days (08-31, 09-01).
- **Compact-card scores are estimates** read from the score bar's height,
  to about +/-0.75 points (+/-1.7 for pattern quality). Exact for
  `tip_n <= 3`.
- **No email for 2026-06-12.**
- **`outcomes` rules** (full detail in `OUTCOMES_SCHEMA.md`): windows count
  trading days present; only bars with volume trigger; same-bar target and
  stop resolve to the stop; splits inside a window are converted to the tip
  date's prices; slippage is a flat 10 bp a leg (trigger prices kept).
- **5m volume is unreliable as a quantity;** use daily `vo x cl` for
  liquidity and slippage.
- **`ac` is adjusted as of the fetch,** so its level includes later splits
  and dividends; ratios between dates are right.
- **Alphanume coverage is partial:** Filing Intensity's pull stopped partway
  (about "KO" alphabetically), and Wikipedia Views were pulled only for
  tickers also in QGMI.
- **Whole-table figures** in eodhd2's own docs (e.g. `OUTCOMES_SCHEMA.md`'s
  same-bar counts) cover in-sample and hold-out together; don't use them as
  results.

## Checks run by the build

- No `tip_exchange`, `tip_details` or `outcomes` row dated after 2026-09-01,
  and no Alphanume row available after it.
- `update_outcomes()` run on the extract changes nothing: the bars
  delivered are enough to reproduce every outcome.
- `window_gaps` and `outcome_gaps` list what's short; nothing is dropped.

Each failure stops the build. The results for this build are below.

## This build: `sda_insample_2026-10-30.db`

**Size and coverage**
- 1.13 GB. 4,059 tips and outcomes; 108,711 daily and 8.5M 5m rows, for
  1,831 and 1,827 codes (SPY included).
- SPY: 142 sessions, 2026-03-10 to 2026-09-30.

**Outcomes:** all 4,059 finished, none pending or open. 4 are from daily
bars (EMF, AWP, CHY, JRS: closed-end funds EODHD has no 5m data for).

**`window_gaps`:** 6.
- `daily`: THR 2026-04-28 (19 sessions), HOOZ 2026-08-13 (17). Both
  investigated in `TICKER_ALIASES.md`; neither is a rename, and both
  outcomes are resolved.
- `intraday_5m`: the 4 closed-end funds above.

**Alphanume, tips with a row available by the tip date:**

| Dataset | Codes with rows | Tips covered (of 4,059) |
|---|---|---|
| `market_cap` | 1,750 | 3,903 |
| `filing_intensity` | 836 | 1,903 |
| `wikipedia_views` | 109 | 352 |
| `risk_regime` | (market-wide) | all; 376 sessions |

**IG files:** 2025-10-06 (1,620 tipped codes listed), 2026-10-22 (1,458),
2026-10-29 (1,456), 2026-10-30 (1,454), by `source_modified`.

**`ticker_aliases`:** 25 rows, of eodhd2's 28: NISN, XWIN and WBD concern only hold-out tips and are left out. `TICKER_ALIASES.md` also drops SATS, whose listed tips are all hold-out; its alias row stays in the table because SATS was also tipped on 2026-05-20, a window that ends before the rename.
