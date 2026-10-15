# Ticker Alias Log

A readable record of every `ticker_aliases` row in production, kept so the
reason behind each one is visible without opening the db. The db row's own
`reason` column is the authoritative copy; this file mirrors it. When you
add an alias with `add_ticker_alias()`, add a line here too.

For how aliases work (fetch-time resolution, `effective_date` as a practical
cutover, why volume isn't rescaled), see `doc/DESIGN_DECISIONS.md`,
"ticker_aliases: smoothing renamed / split / merged / cashed-out tickers".
The first five were researched in detail in
`doc/DELISTING_RESEARCH_2026-09-30.md`.

Snapshot taken from production `sda.db` on 2026-10-14: 14 aliases (16 as of
2026-10-15).

## Renames (new_code, ratio 1.0)

Same listed security under a new symbol; one continuous price series.

| old_code | new_code | effective_date | Added | Tip dates affected | What happened / how verified |
|---|---|---|---|---|---|
| ELWS.US | PDC.US | 2026-01-20 | 2026-10-15 | 2026-05-15 | Earlyworks Co., Ltd. ticker change (last ELWS trade 2026-01-16). 2026-01-16 close 7.06 identical on both. Newsletter still tipped it as ELWS four months later; entry_zone_low 7.08 equals PDC's 2026-05-14 close exactly. |
| FLGC.US | ZSTK.US | 2026-01-29 | 2026-09-30 | 2026-08-27 | Flora Growth Corp. renamed ZeroStack Corp. (last FLGC trade 2026-01-28). 2026-01-28 close 8.95 identical on both symbols. |
| NISN.US | AIOS.US | 2026-02-12 | 2026-10-12 | 2026-09-21 | Ticker-only rename to AIOS Tech Inc. (GlobeNewswire/Finviz). |
| BLBX.US | ALOY.US | 2026-02-25 | 2026-10-14 | 2026-08-13 | Blackboxstocks Inc. renamed REalloys Inc. The newsletter still tipped it as BLBX six months later; the tip's entry_zone_low (12.54) equals ALOY's 2026-08-12 close exactly, so it was charting ALOY under the stale symbol. EODHD serves nothing for BLBX.US, hence effective from the rename date. |
| GMGI.US | MRDN.US | 2026-05-05 | 2026-09-30 | 2026-08-21, 2026-08-27 | 1-for-12 reverse split + rename to Meridian Holdings, real date 2026-03-03; EODHD kept serving split-adjusted GMGI.US through 2026-05-04, so cutover is 05-05. 2026-05-04 close 12.85 identical on both. |
| CUK.US | CCL.US | 2026-05-07 | 2026-10-14 | 2026-04-17 | Carnival DLC unification: Carnival plc (CUK) merged 1-for-1 into Carnival Corporation Ltd (CCL); CUK suspended before open 2026-05-07. The two tracked near-identically while dual-listed. |
| EXPI.US | AGNT.US | 2026-05-08 | 2026-10-14 | 2026-04-21 | eXp World Holdings rebranded AGNT Inc (Delaware → Texas). EODHD's EXPI.US and AGNT.US rows byte-identical through 2026-05-07. |
| XWIN.US | XMAX.US | 2026-05-18 | 2026-10-12 | 2026-09-25, 2026-10-01 | Ticker-only rename to XMax Inc. (formerly Nova Lifestyle) (GlobeNewswire). |
| VSCO.US | VSXY.US | 2026-06-02 | 2026-09-30 | 2026-04-30, 2026-08-25 | Victoria's Secret & Co. voluntary ticker rebrand. 2026-06-01 close 54.30 identical on both. |
| SATS.US | ECHO.US | 2026-06-24 | 2026-10-12 | 2026-10-06, 2026-10-13 | Ticker-only rename, EchoStar Corporation (EchoStar IR). |
| FDP.US | DMC.US | 2026-06-27 | 2026-09-30 | 2026-08-25 | Fresh Del Monte Produce renamed Del Monte Corporation, effective 2026-06-29 (last FDP trade 06-26). 2026-06-26 close 29.22 identical on both. |

## Stock-for-stock mergers (new_code, ratio ≠ 1.0)

Old shares converted into a fixed number of acquirer shares; price after the
boundary is `ratio × new_code`.

| old_code | new_code | ratio | effective_date | Added | Tip dates affected | What happened / how verified |
|---|---|---|---|---|---|---|
| AMWD.US | MBC.US | 5.150 | 2026-05-29 | 2026-10-14 | 2026-04-30 | American Woodmark merged into MasterBrand Corp, completed 2026-05-28. 5.150 × MBC (~9.08–9.31) ≈ 46.8–47.9, consistent with AMWD's 48.09 close during the final arbitrage-spread days. |
| CTRA.US | DVN.US | 0.700 | 2026-05-07 | 2026-10-15 | 2026-05-06 | Coterra Energy acquired by Devon Energy, all-stock, completed 2026-05-07 (Devon 10-Q/8-K). Last CTRA close 32.56 vs 0.70 × DVN 46.60 = 32.62. Outcome unaffected (already `aborted` on 05-06); alias only completes coverage. |
| LBRDA.US | CHTR.US | 0.236 | 2026-08-20 | 2026-09-30 | 2026-08-18 | Liberty Broadband Series A acquired by Charter, effective 2026-08-19. 0.236 × CHTR 152.47 = 35.98 vs LBRDA last close 35.99. |

## Cash takeovers (cash_price, no successor)

Position closed out at a fixed cash amount; flat zero-volume rows at
`cash_price` from `effective_date` on (daily only; see `DESIGN_DECISIONS.md`
on why there's no intraday equivalent).

| old_code | cash_price | effective_date | Added | Tip dates affected | What happened / how verified |
|---|---|---|---|---|---|
| APLS.US | 41.03 | 2026-05-14 | 2026-10-14 | 2026-04-24 | Apellis acquired by Biogen via cash tender ($41.00 + a CVR up to $4.00, excluded since it isn't priced or tradeable). Tender accepted 2026-05-14. Last real close 41.03 matches the deal price. |
| SLNO.US | 53.01 | 2026-05-19 | 2026-10-14 | 2026-04-23 | Soleno acquired by Neurocrine Biosciences, all-cash tender ($53.00), effective 2026-05-18. Last real close 53.01 matches the deal price. |

## Investigated, no alias added

Tips that `unresolved_tips()` flags but where an alias isn't the right fix.
They'll keep appearing in the nightly log's `daily` line; that's expected.

| code | tip_date | Why no alias |
|---|---|---|
| HOOZ.US | 2026-08-13 | Leveraged single-stock ETF, short by 3 days (last data 2026-09-04). Not a rename/merger. Outcome resolved anyway (`aborted`). |
| THR.US | 2026-04-28 | Short by 1 day (last data 2026-05-22). Thermon's real merger is 2026-06-01, after this tip's window, so that isn't the cause. Outcome resolved anyway (`closed_target`). |

## Lessons from the research so far

- EODHD never follows a ticker through a corporate action; it just stops
  serving the old symbol, sometimes weeks or months late (GMGI).
- Verify price continuity at the boundary before trusting an alias: an
  identical close on both symbols for a rename, `ratio × new` ≈ old for a
  merger, last close ≈ deal price for a cash-out.
- A changed *business* isn't a reason to rule an alias out. What matters is
  whether it's the same listed security. Compare the tip's own price levels
  (entry zone, target, stop) with the candidate successor's prices around
  the tip date (BLBX).
