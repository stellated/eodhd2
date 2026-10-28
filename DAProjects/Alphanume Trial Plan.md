# Alphanume Trial Plan

Drafted 2026-10-23 (Melbourne). Covers what to do before and during the
3-day free trial, what each analysis repo gets, and how to decide whether
to subscribe. Times are Melbourne (AEDT, UTC+11) unless marked ET (New
York; EDT, UTC-4, until US clocks change on 1 November).

## Facts this plan rests on (checked 2026-10-23)

- **Trial:** <https://www.alphanume.com/signup?plan=pro&billing=monthly>.
  "Full Pro access for 3 days": every dataset, REST, MCP and dashboard
  exports. It **converts to paid automatically** ($349 monthly, or
  $1,999/year if you pick yearly) unless cancelled before it ends.
- **Fees are non-refundable**, including partial periods.
- **Licence:** "Upon termination, your license to use the Services ends
  immediately." The terms don't say whether data downloaded during a trial
  may be kept and used afterwards. This decides whether a trial can
  replace a paid month (see "Before the trial").
- **API:** JSON only, API key in an `X-API-Key` header, 600 requests a
  minute (HTTP 429 beyond). The terms forbid exceeding rate limits or
  "aggressively" scraping. Many endpoints return up to 50,000 rows per
  response with cursor paging, so a full history pull takes minutes to
  hours, not days.
- **Nothing is reachable without a key:** every endpoint returned "API key
  required" when probed, so no schema or history depth can be checked
  before the trial.
- **Alphanume's reply (Earl, founding partner, 2026-10-23):** "move at your
  own pace and test out the data for as long as you need"; "for a lot of
  our endpoints you can get the full history in 1-3 calls"; "most of our
  coverage is in event-driven catalysts, so the starting date for most
  will be around 2024". It doesn't answer the two other questions: whether
  data may be kept without subscribing, and whether one request a second
  is acceptable.
- **History depths are mostly not published.** Known: Lockup Expirations
  from 2021; Reverse Splits' market cap unavailable before 2024; Short
  Locates only from 2026-10-08. Optionable Tickers is a monthly snapshot
  (first trading day of each month). Ticker Classification is current
  state only (no history).
- **There is a Reverse Splits dataset** (`/v1/capital/reverse-splits`),
  which the Reverse Splits project should use.
- **Publication times** (ET): Pre-Market Drop Risk scores use prices up to
  09:00 (publication time not stated); Next-Day Movers 15:30 for the next
  session; S&P 500 Risk Regime 10:10; QGMI rebalances monthly at 16:05;
  Filing Intensity refreshed nightly at 23:30.

## Before the trial

**Ian:**
1. Email hello@alphanume.com and ask:
   - May data downloaded during a trial or a paid month be kept and used
     for private research after the subscription ends?
   - History start date and typical rows per day for: Next-Day Movers,
     Pre-Market Drop Risk, QGMI, Optionable Tickers, Reverse Splits,
     Dilution, Filing Intensity, Historical Market Cap, Wikipedia Views,
     S&P 500 Risk Regime.
   - When Pre-Market Drop Risk is published each morning.
   - Whether the trial needs a card.
2. Decide monthly or yearly at signup (break-even is about 5.7 months),
   and put a reminder in the calendar 12 hours before the trial ends.
3. Pick a start time (see "Timing").

**This project (eodhd2), to have ready before signing up:**
1. **A raw capture fetcher** (`scripts/get_alphanume.py`, done 2026-10-23,
   offline-tested; untested against real data until the trial): for a given
   endpoint and parameters, page through every result (each endpoint's
   own cursor fields), and store each response unchanged with its
   endpoint, parameters and UTC fetch time. Throttled well below the
   limit (about 60 requests a minute), resumable after a failure, and
   tested offline against the sample responses in alphanume's docs.
   Parsing into proper tables is deliberately left until after the trial:
   during the trial the only job is to capture everything once.
2. **Where raw captures go** (done): a separate `alphanume.db` on mars's
   own disk (`~/data/alphanume.db`), copied to OneDrive after every run
   (`ops/run_alphanume.sh`) but never pulled back, so a history pull
   lasting hours and the live timers can write at the same time. This
   keeps `sda.db` (about 1 GB) from growing with raw JSON. Parsed tables
   (e.g. `ndm_picks`) join `sda.db` later, as the NDM design decision
   says.
3. **Live capture timers** (templates done; install and enable just before
   the trial, disable after): Pre-Market Drop Risk at 09:05 and 09:35 ET,
   Risk Regime at 10:15 ET, Next-Day Movers at 15:35 and 16:35 ET. These
   record what was published on the day, to compare with the history
   pulled later ("point-in-time, never revised" is a claim to test, not
   assume). Two captures a day for two of them also show whether
   anything changes soon after publication.
4. **A pull list:** for each project, the exact endpoints and parameters
   for the full history (below), so day 1 is only "run it".
5. **Check what's needed from EODHD and estimate the call budget** (done,
   `scripts/eodhd_budget.py`): turns a pull into an EODHD fetch list and a
   call/storage estimate, and lists tickers EODHD's symbol lists don't
   know. Run it on day 2:
   `eodhd_budget.py fetch-symbols ~/data/eodhd_symbols` (2 calls), then
   `eodhd_budget.py --db-path ~/data/alphanume.db --symbols
   ~/data/eodhd_symbols/*.csv --fetch-list fetch_list.csv`.
   EODHD's limit is 100,000 calls a day (an intraday request costs 5), so
   calls are unlikely to be the constraint; storage for 1m pre-market data
   may be.

Not needed before the trial: SOFR/RBA rates history, dividends (on hold),
parsing tables, anything on the analysis side.

## Timing

**Recommended: start early Wednesday 28 October**, e.g. 06:00 Melbourne
(Tuesday 15:00 ET). The 72 hours then give three live captures of each
daily dataset:

| Capture | Dates (ET) |
|---|---|
| Next-Day Movers at 15:35 and 16:35 | Tue 27, Wed 28, Thu 29 |
| Pre-Market Drop Risk at 09:05 and 09:35 | Wed 28, Thu 29, Fri 30 |

The trial ends Saturday 31 October about 06:00 Melbourne (Friday 15:00
ET). The captures run on timers, so you don't need to be awake for them.
The final tip backfill (Monday 26 October 04:00) is finished by then.

**The weekend of 1 November is a poor alternative:** a trial starting
Saturday morning Melbourne (Friday afternoon ET) contains only one US
session (Monday 2 November), so only one live capture of each dataset.
If the fetcher isn't ready by Tuesday, the next good slot is another
weekday morning, e.g. Wednesday 4 November.

## During the trial

Ian does the steps that need the account, sudo or a decision; Claude
(in an eodhd2 session) does the rest. Once the key is in `.env`, tell
Claude "key's in" and it carries on from Day 1 step 3. Scripts read the
key from `.env` themselves, so Claude never needs to see it.

**Day 1 (Wednesday morning Melbourne):**
1. **Ian:** sign up, create the API key, store it in `.env` as
   `ALPHANUME_API_KEY` (`.env` is gitignored). A new key can take a few
   minutes to activate (per the API's own 401 message).
2. **Ian:** one test capture, then enable the live timers (both need sudo):
   ```
   sudo systemctl start eodhd-alphanume-live@ndm.service
   journalctl -u eodhd-alphanume-live@ndm.service -n 30
   sudo systemctl enable --now eodhd-alphanume-premarket.timer eodhd-alphanume-regime.timer eodhd-alphanume-ndm.timer
   systemctl list-timers 'eodhd-alphanume-*'
   ```
   The log should end with "copied to onedrive:...alphanume.db".
   
    When the key's in and the timers are on, tell me (Claude) "key's in".
3. **Claude:** call each endpoint once (a scratch script; `get_alphanume.py`
   has no probe command); record the field list, earliest and latest
   dates, and paging behaviour, and check them against `JOBS` in
   `get_alphanume.py`. Fix the script first if an endpoint differs from
   the docs.
4. **Claude:** run the full history pulls in the background
   (`ops/run_alphanume.sh pull JOB ...`; the pull list is below), in
   order of value:
   Next-Day Movers, Pre-Market Drop Risk, QGMI, Optionable Tickers,
   Reverse Splits, Dilution, Historical Market Cap, Filing Intensity,
   Risk Regime, Wikipedia Views, Ticker Classification.
   Each dataset is looked at as it lands.
5. **Claude:** record row counts, date ranges and distinct tickers per
   dataset (`get_alphanume.py summary`). Check the date field names
   against `DATE_FIELDS` in `eodhd_budget.py`, and whether NDM's `date`
   is the pick date or the trade date (settled by the first live NDM
   capture, 15:35 ET on day 1). Report back to Ian.
6. **Ian, optional:** connect the hosted MCP server in an analysis
   session to try it out (OAuth in a browser; not used by this pipeline).

**Day 2 (Thursday morning Melbourne), all Claude, reported to Ian:**
1. Check the first live captures arrived.
2. Data-quality checks on the pulls:
   - gaps in dates (trading days with no rows);
   - duplicates;
   - tickers EODHD doesn't know (a measure of how much rename and
     delisting work each project faces), and the EODHD call and storage
     estimate: both from `eodhd_budget.py`;
   - IG coverage via `ig_margins_resolved` (current file, see the
     handover caveat): what share of picks are tradeable, and
     for the short projects shortable, and at what borrow rate.
3. A first look at each dataset's own outcome fields (they publish
   forward returns), e.g. how often Pre-Market Drop Risk's top-ranked
   names really fell 5% or more versus the whole list; check a sample of
   those returns against EODHD daily bars.

**Day 3 (Friday morning Melbourne, last full day):**
1. **Claude:** pull the recent history again for the live-capture dates
   and compare with the live captures: same tickers, ranks and scores?
2. **Claude:** fill any gaps in the pulls.
3. **Claude:** a go/no-go summary against the criteria below.
4. **Ian:** decide go/no-go. If no-go, cancel before about 06:00
   Saturday. Either way, disable the live timers once the trial ends:
   ```
   sudo systemctl disable --now eodhd-alphanume-premarket.timer eodhd-alphanume-regime.timer eodhd-alphanume-ndm.timer
   ```

## Day 1 results (2026-10-27, New York time)

Every endpoint matched the docs' field names; every history pull
completed with no HTTP errors (`~/data/alphanume.db`, 560 MB).

| Dataset | Rows | From | To | Tickers | Shape |
|---|---|---|---|---|---|
| Next-Day Movers | 3,435 | 2024-01-31 | 2026-10-26 | 166 | 5 picks every day |
| Pre-Market Drop Risk | 6,408 | 2024-05-10 | 2026-10-27 | 1,101 | ~10 a day, up to 27 |
| QGMI | 1,180 | 2017-01-03 | 2026-10-01 | 212 | 10 a month (first session) |
| Optionable Tickers | 343,754 | 2019-01-02 | 2026-10-01 | 6,063 | monthly, ~3,600 a month |
| Reverse Splits | 3,986 | 2022-10-03 | 2026-10-27 | 3,069 | ~3 a day, many OTC (...F/...Y) |
| Dilution | 926 | 2025-01-03 | 2026-10-26 | 511 | ~2 a day |
| S&P 500 Risk Regime | 3,916 | 2011-03-30 | 2026-10-27 | | 0/1; 1 on 29% of days |
| Ticker Classification | 5,634 | | | 5,634 | one snapshot |
| Historical Market Cap | 2.6M | 2015-09-30 | 2026-10-26 | 1,991 | 1,843 of 3,834 tickers empty |
| Filing Intensity | 2.6M | 2020-01-02 | 2026-10-26 | 1,501 | 2,333 of 3,834 empty |
| Wikipedia Views | 138,783 | 2024-01-02 | 2026-10-26 | 142 | 70 of 212 empty |

- **NDM's `date` is the pick date.** Picks dated 10-27 appeared after
  15:30 on 10-27; 10-26's returns were filled in by 16:35, after the
  next session's close.
- **Every dataset's date field is `date`**; `DATE_FIELDS`' other names
  just fall through to it.
- **Empty per-ticker results** are nearly all Reverse Splits' OTC and
  long-delisted names; the strategy datasets' tickers are covered (at
  most 3% empty).
- **Risk Regime: 0 = risk-on, 1 = risk-off** ("elevated volatility and
  stress"), from "forward-looking implied volatility metrics", not
  moving averages or a VIX threshold; published 10:10 ET each trading day
  (alphanume's sp500-risk-regime guide; model and horizon not stated).
  Against EODHD's VIX close: mean 16.4 on 0 days, 22.3 on 1 days; every
  crisis day checked is 1 (2020-03-16 at VIX 82.7, 2024-08-05, 2025-04-08);
  no single VIX level matches it more than 75% of the time. It switches
  93 times in 15.6 years (runs of about 6 weeks). So it's a stress
  flag, not a bear-market flag: 47% of 2022's days, 41% of 2025's, 6% of
  2017's. A date's label is only known from 10:10 that day, so anything
  entering at or before the open must use the previous session's label.
- **NDM and Pre-Market Drop start after October 2020,** so 1m data is
  only needed for Pre-Market Drop's pre-market hours.
- **EODHD budget** (`eodhd_budget.py`, real data): 3,834 tickers, 18,848
  calls with merged spans or 56,118 one request per event day (limit
  100,000 a day), about 0.9 GB, mostly Pre-Market Drop's 1m days. 179
  tickers aren't in EODHD's symbol lists (156 of them Reverse Splits OTC
  names; 17 Pre-Market Drop, 6 Dilution).
- **Fixes:** per-ticker jobs make one request per ticker, not one per
  year (1b7bdfe); the pull no longer holds the write lock during a
  request (41467e4), which had made the 15:35 NDM capture fail with
  "database is locked" (rerun by hand at 15:54).

## Pull list

| Project | Alphanume datasets | EODHD data |
|---|---|---|
| QGMI | QGMI constituents; Optionable Tickers (all monthly snapshots); S&P 500 Risk Regime; Wikipedia Views; Filing Intensity; Historical Market Cap; Ticker Classification | Daily for each constituent for its index month plus the prior year |
| NDM ORB | Next-Day Movers | 5m for each trade date (pick date + 1 session) plus daily for the 20 prior days; before October 2020, 1m data aggregated to 5m (see below) |
| Pre-Market Drop | Pre-Market Drop Risk | Daily for the 20 prior days; 1m data (includes pre-market) or 5m for the trade date |
| Reverse Splits | Reverse Splits | Daily ±30 days around each split; EODHD's splits endpoint for cross-checking |
| Dilutive S-1 | Dilution; Filing Intensity; Historical Market Cap | Daily for 1 year before and 30 days after each filing |
| SDA tips | S&P 500 Risk Regime (to compare with the newsletter's regime colours) | Already in `sda.db` |

**Caveat on 1m data aggregated to 5m** (spot check of 13 ticker-days,
2026-10-24; details in `doc/DESIGN_DECISIONS.md`, "EODHD 1m bars
aggregated to 5m"): opens and closes match EODHD's own 5m bars, and most
highs and lows do too, but the 1m feed keeps bad prints the 5m feed
filters out (e.g. TSLA 2021-03-05 high 600.00 against 571.27 in 5m). So:
- use 5m wherever it exists (from October 2020);
- for NDM before then, aggregated 1m is fine for open-to-close returns,
  but the ORB and any stop need a spike filter on the 1m highs and lows;
- the 09:30 bar's volume in 1m is often half of 5m's (the opening
  auction, probably), so don't use it for liquidity;
- 5m is regular hours only, so Pre-Market Drop needs 1m anyway, and
  gets the same spike filter.

## Handover to the analysis repos

One Claude overseeing six repos: SDA, QGMI, NDM ORB, Pre-Market Drop,
Reverse Splits and Dilutive S-1. Each gets:

- **A dated, read-only db extract** with only the tables it needs, plus a
  note of which pull and which `sda.db` copy it came from.
- **A schema doc** like `doc/OUTCOMES_SCHEMA.md`: column meanings, how
  rows were produced, known caveats.
- **Data caveats:** the relevant parts of `doc/LIMITATIONS.md` (5m
  volume, padded vo=0 rows, unadjusted prices, ticker aliases), and the
  result of the live-vs-history check for its dataset.
- **IG coverage** through `ig_margins_resolved` (built 2026-10-23), which
  follows renamed tickers. Caveats (`doc/LIMITATIONS.md` §6): IG keeps
  no history, so the snapshots are 2022-01, monthly 2023-08 to 2024-04,
  2025-10 and monthly from 2026-10; a pick is matched to the nearest one,
  not to its own date; and only researched renames have aliases.
- **A README** with Ian's main question and data list from
  `Alphanume Trading Strategies.md`.

Shared code across the six (IG cost model with financing and borrow,
liquidity estimators, db access) needs a home: either a small seventh
library repo, or one of the six that the others import from. To decide
before the first analysis repo is set up.

**SDA specifically:** the in-sample extract (tips up to the split date,
with every later tip and outcome physically deleted), `OUTCOMES_SCHEMA.md`,
`TICKER_ALIASES.md`. `outcomes` stays here as the baseline simulation;
variants are built on the analysis side.

## Go/no-go for a paid subscription

Go if all of these hold:
1. **Licence:** the answer on keeping data is acceptable, or the cost of
   staying subscribed for as long as the analysis needs the data is.
2. **History depth:** enough for the main questions. QGMI's
   cherry-picking question needs several years including a bear market
   (2022); NDM and the short projects need at least about two years to
   give a useful number of trades.
3. **Point-in-time integrity:** the live captures match the re-pulled
   history for the same dates.
4. **Tradeability:** a useful share of picks is tradeable at IG, and for
   the short projects shortable at a borrow rate that leaves an edge.
5. **Sample size:** picks per day × history days gives enough trades
   after the IG and liquidity filters.
6. **Early signal:** the datasets' own forward-return fields, spot-checked
   against EODHD, look worth testing properly (not proof, just not
   obviously nothing).
7. **EODHD budget:** the call and storage estimate fits the daily quota
   over a reasonable number of weeks.

**Whether a paid month is needed:** if the trial captures every history
and the licence lets you keep using it, no. If the licence doesn't, the
question becomes how long the analysis needs, and monthly versus yearly.
A paid month also helps if the trial shows something needing more than
three days of live captures to settle (e.g. revisions to history).
