# `outcomes` Table: Schema and Semantics

Reference for anyone analysing `sda.db` without this repo's history, written
in particular for an analysis-project LLM session. It says what each column
means and what rules produced it. The rationale behind each rule is in
`doc/DESIGN_DECISIONS.md` ("outcomes design" and the entries after it);
`doc/LIMITATIONS.md` covers the tip data itself.

## What a row is

One row per tipped `(code, tip_date)`: the trade a simple, mechanical robot
would have made from the tip. It buys when price touches the entry zone,
then sells at the target, at the stop, or at the end of the holding period.
Everything is simulated from 5-minute bars (`intraday_5m`).

- **Primary key:** `(code, tip_date)`.
- **Join to the tip:** `tip_details` on `(exchange, tip_date, tip_n)`. The
  tip's own inputs (entry zone, target, stop, holding periods, scores,
  win probability, sector) live there and deliberately aren't copied here.
- **Duplicates:** if a ticker was tipped more than once on the same day
  (e.g. in both the NASDAQ and NYSE emails), only the lowest `tip_n` (the
  newsletter's better-ranked slot) gets a row.
- **Reproducible:** rows are a deterministic function of `tip_details` and
  the price tables. The whole table was rebuilt from scratch on 2026-10-16
  (`DELETE FROM outcomes`, then `update_outcomes()`); running it again
  changes nothing.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `code` | TEXT | EODHD ticker with exchange suffix, e.g. `AVGO.US`. Always the symbol as tipped, even if later renamed (see "Ticker aliases"). |
| `tip_date` | TEXT `YYYY-MM-DD` | Date the tip email arrived (before the US open). Day 0 of the entry window. |
| `exchange` | TEXT | Which newsletter edition: `NASDAQ` or `NYSE`. Part of the join key, not necessarily the listing exchange. |
| `tip_n` | INTEGER | 1-based position of the tip in that email. 1-3 are "full" cards, 4-20 "compact". |
| `status` | TEXT | Outcome; see "Status values". |
| `buy_date` | TEXT | Local (New York) date of the entry bar. NULL if never entered. |
| `buy_timestamp` | INTEGER | UTC Unix epoch of the entry bar's **start**. |
| `buy_local_time` | TEXT `HH:MM:SS` | New York time of the entry bar's start (`09:30:00` = first bar). |
| `buy_trigger_price` | REAL | Price the entry rule filled at, before slippage. |
| `buy_price` | REAL | `buy_trigger_price` plus slippage (what the trade paid). |
| `buy_bar_volume` | INTEGER | Volume of the entry bar, kept for liquidity/slippage studies. |
| `risk_per_share` | REAL | `buy_price - stop`: the planned loss per share. Positive. |
| `sell_date`, `sell_timestamp`, `sell_local_time` | | As for buy, for the exit bar. NULL unless `closed_*`. |
| `sell_trigger_price` | REAL | Price the exit rule filled at, before slippage. |
| `sell_price` | REAL | `sell_trigger_price` minus slippage. |
| `sell_bar_volume` | INTEGER | Volume of the exit bar. |
| `profit_per_share` | REAL | `sell_price - buy_price`. |
| `r_multiple` | REAL | `profit_per_share / risk_per_share`. A full stop-out is about -1.0 (a bit worse after slippage, much worse after a gap through the stop); +1.0 is a gain equal to the planned risk. |
| `fill_granularity` | TEXT | `5m` (normal) or `1d`; see "Daily-bar fallback". NULL only on not-yet-checked `pending` rows. |
| `same_bar` | TEXT | Set when one bar touched more than one trigger level; see "Same-bar events". NULL otherwise. |

Prices are in the security's trading currency (USD), unadjusted, as EODHD's
intraday feed provides them.

## Status values

| Status | Terminal? | Meaning |
|---|---|---|
| `pending` | no | Not entered yet; entry window still open. |
| `open` | no | Entered; holding period not over yet. |
| `aborted` | yes | Price reached the stop **inside the entry window before any entry**: the setup broke down. No trade. |
| `expired_unfilled` | yes | Entry window ended with neither an entry nor a stop breach. No trade. |
| `closed_target` | yes | Exited at the target. |
| `closed_stop` | yes | Exited at the stop. |
| `closed_time` | yes | Held to the end of the holding period; sold at that day's last real close. |

**For statistics, use terminal rows only.** `pending`/`open` rows are recent
tips still being tracked; the weekly job recomputes them and they change.
Trades are the three `closed_*` statuses. `aborted` and `expired_unfilled`
are non-trades, but they're informative and shouldn't be dropped silently
(dropping them would overstate the fill rate).

## The rules that produce a row

### Windows (trading days counted as dates present in the data)
- **Entry window:** days 0..`holding_period_low`, with day 0 = `tip_date`.
  E.g. `holding_period_low=2` means entry is allowed on the tip day and the
  next two trading days.
- **Holding period:** days 0..`holding_period_high`, with day 0 =
  `buy_date`. Target/stop only count inside it. Otherwise the position is
  sold at the close of day `holding_period_high`.
- Bars outside these windows never trigger anything, even though the price
  table holds bars beyond them (from other tips of the same ticker). Before
  2026-10-16 this wasn't enforced, which held positions too long and let
  entries happen after the window; that's why the table was rebuilt.
- `expired_unfilled` and `closed_time` are only decided once the data shows
  the window is over (a later trading day exists), so neither can be decided
  early because of missing data. Target, stop and abort are decided as soon
  as the triggering bar exists.

### Only bars with volume count
A bar with `vo = 0` has no real trade, whether EODHD returned nothing for
that slot or returned a row with blank prices. It never triggers an entry,
an exit, or the time-exit price. Trades wait for the next bar that has
volume. Day counting for the windows still includes such days.

### Fill prices
- **Entry:** the first real-volume bar in the entry window whose range
  overlaps `[entry_zone_low, entry_zone_high]`. It fills at the bar's open if
  the bar opened inside the zone, otherwise at the zone edge it was reached
  from (`entry_zone_high` falling from above, `entry_zone_low` rising from
  below).
- **Target / stop:** fill at the level if crossed during the bar, or at the
  bar's open if it gapped past it. A gap through the target fills better
  than the target; a gap through the stop fills worse than the stop (so gap
  risk is already in `r_multiple`).
- **Time exit:** the close of the last real-volume bar on the deadline day.
- **Slippage:** a flat 10 basis points (0.10%) per side, always against the
  trade: buys pay more, sells receive less. The `*_trigger_price` columns
  hold the prices before slippage, so a different slippage model (e.g. one
  based on `*_bar_volume`) can be applied without re-simulating.

### Same-bar events (`same_bar`)
With only 5-minute OHLC there's no way to know the order of prices inside a
bar. When one bar touches more than one trigger level, the **worst case for
the trade** is assumed and the row is tagged:

| `same_bar` | What the bar did | Result assumed |
|---|---|---|
| `entry_target` | Entry bar also reached the target | Entered, then exited at target on that bar (no stop involved, so no conflict) |
| `entry_stop` | Entry bar also reached the stop | Entered, then stopped out on that bar (`closed_stop`) |
| `entry_target_stop` | Entry bar reached zone, target and stop | Entered, then stopped out (`closed_stop`) |
| `target_stop` | A later bar reached both target and stop | Stopped out (`closed_stop`) |
| NULL | No same-bar event | |

One exception: a bar that **opens at or below the stop** gives `aborted` even
if it later rallies into the entry zone, because the setup was broken before
any trade happened. These rows have `same_bar` NULL.

`entry_stop` covers both a bar that opened inside the zone (entry certain)
and one that fell through the zone into the stop (entry assumed). Counts
are small (as of 2026-10-16: 20 `entry_target`, 1 `entry_stop`, no others),
but use the column to exclude or test these rows rather than trust them.

### Daily-bar fallback (`fill_granularity = '1d'`)
A few tickers (closed-end funds) have no intraday data at EODHD at all. If
EODHD has confirmed that for the tip (a row in `intraday_unavailable`) and
the tip's window has closed, the row is simulated from `daily` bars
instead. A daily bar spans a whole session, so same-bar events are far more
likely and timing is coarse. Treat these as lower fidelity; usually exclude
them or analyse them separately. As of 2026-10-16 there are 4 (EMF, CHY,
AWP, JRS, all April 2026).

## Things the analysis should know

- **Coverage:** tips from 2026-04-08 onward. The historical backfill fills
  in 2026-05-20..2026-08-04 by about 2026-10-26; until then that range has
  no tips at all.
- **Ticker aliases:** when a tipped ticker was renamed, merged or bought for
  cash, prices after the event come from the successor (scaled by the
  merger ratio) or are flat at the cash price, but still stored under the
  tipped `code`. The list and reasons are in `doc/TICKER_ALIASES.md` and the
  `ticker_aliases` table. Volume isn't rescaled across an alias boundary.
- **Tip-side caveats** (`tip_details`, see `doc/LIMITATIONS.md`): the four
  `*_score` columns are exact for `tip_n` 1-3 but estimated (about ±0.75-1.7
  points) for 4-20. `expected_return` only exists from 2026-08-31.
  `expected_reward`/`expected_risk` are NULL for compact cards from that date.
- **Survivorship:** nothing is dropped for a ticker that later stopped
  trading. Tips with permanently short price data (HOOZ 2026-08-13, THR
  2026-04-28) still have outcomes.
- **Read-only:** analyse a dated copy of `sda.db` and record which copy was
  used. Never write to the production file; the pipeline's sync assumes it
  is the only writer.
