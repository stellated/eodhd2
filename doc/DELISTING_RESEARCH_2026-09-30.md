# Delisting Research — 2026-09-30

Background: `unresolved_tips()` (see `doc/DESIGN_DECISIONS.md`) surfaced five
tips whose post-tip-date price window will never fill in from EODHD:
GMGI.US, FDP.US, VSCO.US, FLGC.US, LBRDA.US. The assumption going in was
that these were delistings — bankruptcy, compliance failure, or a buyout.
This doc researches what actually happened to each, via web search, with a
focus on the price outcome a holder at the cutoff date should be assumed to
have received.

**Headline finding: none of these five is a bankruptcy or total-loss event.**
Four are pure ticker/name changes (with one also being a Nasdaq-compliance
reverse split) with full continuity of share value; one is a completed,
fully-paid stock-for-stock merger. In every case, EODHD's price series simply
stops under the old ticker with no forward-pointer to the new one — this is
a **data-vendor ticker-continuity gap**, not a real financial loss for
anyone who held the stock. See "Implication for this codebase" at the end.

---

## GMGI.US (Golden Matrix Group, Inc.)

**What happened:** 1-for-12 reverse stock split combined with a corporate
rename to "Meridian Holdings Inc." and a ticker change from GMGI to MRDN,
effective 12:01am ET 2026-03-03 (per the company's own 8-K, CIK 1437925).
151.7M shares consolidated into ~12.6M. EODHD's GMGI.US data actually runs
through 2026-05-04 — about two months *after* the real ticker change — which
looks like a data-vendor lag in retiring the old symbol alias, not a second
later event.

**Why:** Nasdaq-compliance move — the stock had fallen below the $1.00
minimum bid price requirement, with a June 30, 2026 deadline to regain
compliance. The rename reflects the company's shift to operating primarily
as a holding company for MeridianBet Group (sports betting/casino gaming).

**Price outcome:** No loss. 12 old GMGI shares → 1 new MRDN share (cash only
for any fractional remainder); "stockholder percentage ownership interest
and proportional voting power will remain virtually unchanged" (company's own
language). MRDN traded ~$9.40–$12.10 through May 2026, bracketing GMGI's last
EODHD print of $12.85 almost exactly — consistent with a clean, value-neutral
transition. **Treat as ticker continuation, not an exit or loss.**

Sources:
- https://www.sec.gov/Archives/edgar/data/1437925/000147793226001129/mrdn_8k.htm
- https://www2.newsfilecorp.com/release/286364/Meridian-Holdings-MRDN-New-Name-New-Ticker-What-To-Expect-Next
- https://app.edgar.tools/companies/0001437925

---

## FDP.US (Fresh Del Monte Produce Inc.)

**What happened:** Pure corporate name and ticker change. Fresh Del Monte
Produce Inc. renamed to "Del Monte Corporation," ticker FDP → DMC, effective
2026-06-29 (last day trading as FDP: 2026-06-26, matching EODHD's cutoff
exactly). Same CIK (0001047340), same shares outstanding, no conversion
ratio.

**Why:** On 2026-03-19 the company completed a $310.2M acquisition of Del
Monte Foods' branded assets (Del Monte®, S&W®, Contadina®) out of Del Monte
Foods' own Chapter 11 bankruptcy (a *different*, previously separately-owned
company — not FDP's own bankruptcy). This reunited the "Del Monte" brand
under one owner for the first time in ~40 years, prompting the rename to
reflect the expanded identity.

**Price outcome:** No loss, no gap. Same registrant, same shares — the
correct backtesting treatment is to splice FDP's series directly onto DMC's
from 2026-06-29 onward as one continuous instrument.

Sources:
- https://freshdelmonte.com/news/fresh-del-monte-produce-inc-announces-name-change-to-del-monte-corporation-and-nyse-ticker-symbol-change-to-dmc/
- https://secure.businesswire.com/news/home/20260609093698/en/Fresh-Del-Monte-Produce-Inc.-Announces-Name-Change-to-Del-Monte-Corporation-and-NYSE-Ticker-Symbol-Change-to-DMC
- https://www.sec.gov/Archives/edgar/data/0001047340/000104734026000042/fdp-20260626.htm

---

## VSCO.US (Victoria's Secret & Co.)

**What happened:** Voluntary ticker symbol change from VSCO to VSXY,
effective 2026-06-02 (last day as VSCO: 2026-06-01, matching EODHD's
cutoff). Per the company's SEC Form 8-K: "no action is required by
shareholders," stock "will continue to trade on the New York Stock
Exchange," CUSIP unchanged.

**Why:** Pure rebrand under new CEO Hillary Super ("VSXY" = "very sexy"),
not financial distress. Coincidentally announced the same morning as a
strong Q1 FY2026 earnings beat + raised guidance — the ~50% share price jump
that day was the earnings reaction, unrelated to the ticker change itself.

**Price outcome:** No loss, uninterrupted trading, same CUSIP. Splice VSCO's
series onto VSXY.US from 2026-06-02 onward.

Sources:
- https://www.sec.gov/Archives/edgar/data/0001856437/000185643726000007/ex991vscomay212026pressrel.htm

---

## FLGC.US (Flora Growth Corp.)

**What happened:** Corporate rename to "ZeroStack Corp.," ticker FLGC → ZSTK,
effective at market open 2026-01-29 (last day as FLGC: 2026-01-28, matching
EODHD's cutoff). Board approved 2025-10-23, shareholders approved
2025-12-19, publicly announced 2026-01-27 (explaining the volume spike that
day — 1.9M shares, ~18% price drop — a market reaction to the
announcement/pivot, not distress or a halt).

**Why:** Business-strategy pivot from a struggling small-cap cannabis/CBD
company (which had already done two prior reverse splits: 1-for-20 in 2023,
1-for-39 in 2025) into an "AI-focused asset management company" holding a
"0G treasury strategy" (crypto/AI infrastructure token). This reads as a
speculative rebrand of a distressed shell — worth noting as the one case in
this group with real prior distress signals (two reverse splits) — but the
January 2026 event itself was still just a rename, not a delisting.

**Price outcome:** Per the company's own 8-K: "Outstanding share
certificates... are not affected by the name change and will continue to be
valid." No split, no cash-out, no conversion ratio at this specific event.
Splice onto ZSTK.US from 2026-01-29 if EODHD carries it; if not, this is
better classified as "unresolved due to ticker change" than a loss.

Sources:
- https://www.sec.gov/Archives/edgar/data/1790169/000106299326000485/form8k.htm
- https://www.barchart.com/story/news/37247160/flora-growth-corp-announces-name-change-to-zerostack-corp-furthering-its-strategy-as-an-ai-focused-asset-management-company

---

## LBRDA.US (Liberty Broadband Corporation, Series A)

**What happened:** Completed, all-stock merger — Liberty Broadband acquired
by Charter Communications (CHTR), effective 2026-08-19 (matching EODHD's
cutoff exactly; the zero-volume final day reflects a trading halt for the
conversion, not a data gap). LBRDA/LBRDB/LBRDK delisted from Nasdaq; Liberty
Broadband became a wholly-owned Charter subsidiary and terminated SEC
reporting. Closed alongside Charter's separate Cox Communications
acquisition.

**Why:** Long-pending combination (definitive agreement first signed
November 2024, ~$17.05B at signing, revised terms ~May 2026). Liberty
Broadband was largely a holding-company wrapper around a large Charter
stake; folding it into Charter eliminated the holding-company discount and
simplified governance.

**Price outcome:** Each LBRDA share converted to a fixed **0.236 shares of
Charter (CHTR) common stock** (cash only for fractional shares, no collar).
Cross-check: 0.236 × CHTR's actual close that day ($152.47) ≈ $35.98 —
matches LBRDA's last close of $35.99 almost exactly, strongly confirming the
mechanism and that this was a fair-value conversion, not a loss. For a
single-exit-price backtest, $35.99 is a well-supported value; for a
carry-forward model, convert to 0.236 CHTR shares at the post-close price.

Sources:
- https://www.panabee.com/news/liberty-broadband-completes-merger-with-charter-shares-convert-at-0-236-ratio
- https://lightreading.com/cable-technology/charter-wraps-cox-and-liberty-broadband-transactions
- https://www.tipranks.com/news/company-announcements/charter-communications-announces-merger-with-liberty-broadband-2
- https://stockanalysis.com/stocks/chtr/history/

---

## Implication for this codebase

`unresolved_tips()`'s current docstring and its `DESIGN_DECISIONS.md` entry
both frame these as "likely delisted/merged/halted... data will very likely
never complete" — technically true (EODHD's series really does stop), but
the implied conclusion ("assume this is a bad/lost outcome") is **wrong for
all five** researched here. Every one is either a value-neutral ticker
rename (4 of 5) or a fair-value-paid merger (1 of 5) — not a single one
should be scored as a loss in a backtest, and none should be silently
excluded as "unresolvable."

**Resolved (2026-09-30):** implemented as the `ticker_aliases` table +
`add_ticker_alias()` + `_fetch_daily_resolved()` in `src/eodhd_io.py` — see
`doc/DESIGN_DECISIONS.md` ("ticker_aliases: smoothing renamed / split /
merged / cashed-out tickers") for the full design, including a third mode
(a fixed `cash_price`, for a pure cash-for-scrip takeover, which none of
these five needed but which came up in discussion). `unresolved_tips()`'s
docstring was corrected to stop implying "likely lost." All five tickers
above were seeded as aliases the same day, verified against a live EODHD
fetch of each successor ticker (GMGI.US→MRDN.US, FDP.US→DMC.US,
VSCO.US→VSXY.US, FLGC.US→ZSTK.US, all ratio 1.0; LBRDA.US→CHTR.US, ratio
0.236) — each showed an exact or near-exact price match with the original
ticker's last known value right at the boundary, confirming the mechanism
and the ratios above.
