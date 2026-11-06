#!/usr/bin/env python3
"""The screen for jml's sample (jml/proposals/eodhd2-jml.md): every US
common stock trading at each period's start, by 60-session median
turnover, before anything is drawn.

  jml_screen.py fetch  --db-path ~/data/jml.db --alphanume-db ~/data/alphanume.db
  jml_screen.py report --db-path ~/data/jml.db --alphanume-db ~/data/alphanume.db
  jml_screen.py draw   --db-path ~/data/jml.db
  jml_screen.py daily  --db-path ~/data/jml.db

`fetch` stores, in jml.db (on mars, not sda.db):
- `sessions`: SPY's trading days, 2014 to 2025 (1 call);
- `symbols`: EODHD's current and delisted US symbol lists (2 calls);
- `bulk_eod`: EODHD's bulk end-of-day for every session the screen needs
  (100 calls each): the 60 sessions up to each screen date, and the 5
  sessions up to 5 years before each period start.
It's resumable (dates already stored are skipped) and stops before
apiRequests passes QUOTA_STOP.

`report` writes `eligible` (one row per eligible stock and period) and
`screen_dropped` (why the rest went), and prints the counts per band.

Venue: the symbol lists give a stock's current or last exchange, not its
exchange on the screen date. A stock listed in 2019 that later fell to
the pink sheets is PINK now, so filtering on exchange would drop the
failures (survivorship). Listed venues are kept; an OTC-labelled common
stock is kept if its ISIN is American (venue_inferred = 1), and dropped
if it's foreign (foreign shares traded OTC).

Five years: a stock qualifies if it traded on any of the 5 sessions up to
the date 5 years before its period start. EODHD files history under the
code it serves today (FB's 2019 bars are under META), so this matches
the stock's own daily series.
"""
import argparse
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from eodhd_io import EODHD_BASE, _redact_token, fetch_daily, pandas2sqlite  # noqa: E402

# (period, first day, last day); Ian, 2026-11-05
PERIODS = [
    (1, date(2019, 11, 1), date(2019, 12, 31)),
    (2, date(2020, 2, 1), date(2020, 3, 31)),
    (3, date(2020, 4, 1), date(2020, 5, 31)),
    (4, date(2022, 4, 1), date(2022, 6, 30)),
    (5, date(2023, 8, 1), date(2023, 9, 30)),
    (6, date(2025, 4, 1), date(2025, 4, 30)),
]
SCREEN_SESSIONS = 60
FIVE_YEAR_SESSIONS = 5
MIN_PRICE = 2.0
MIN_TURNOVER = 1e6
BANDS = [("above_50m", 50e6), ("10m_50m", 10e6), ("1m_10m", 1e6)]  # lower bounds
LISTED = {"NASDAQ", "NYSE", "AMEX", "NYSE MKT", "NYSE ARCA", "BATS"}
QUOTA_STOP = 85_000
SEED = 20261105
# per band: (in-sample, hold-out); Ian, 2026-10-30 and 2026-11-05
DRAW_SIZES = {"above_50m": (40, 13), "10m_50m": (30, 10), "1m_10m": (30, 10)}
HOLD_OUT_SHARE = 0.25  # of stocks, not of slots
FETCH_MARGIN_DAYS = 14  # calendar days either side of a stock-period's span
DUP_CLOSE_TOL = 0.001
DUP_MIN_SHARE = 50 / 60  # of the dates
# Common stocks with bulk bars but in neither symbol list (checked by hand,
# 2026-11-05: EODHD's search has no US entry for them either). Every other
# unlisted code passing the screen was foreign OTC, an ETF or a preferred.
UNLISTED_COMMON = {"CRNX": "Crinetics Pharmaceuticals", "HLX": "Helix Energy Solutions",
                   "LUNA": "Luna Innovations", "NKLAQ": "Nikola", "TBPH": "Theravance Biopharma",
                   "USEG": "U.S. Energy Corp"}
# Bulk prices that are plainly wrong: HLAN closes at about 19,800 in 2022.
BAD_DATA = {"HLAN"}
# Typed "Common Stock" by EODHD but not a company's shares (Ian, 2026-11-06:
# exclude). Checked by hand against every eligible name, 2026-11-06. BDCs,
# royalty trusts, MLPs and REITs are kept: they're businesses in their own right.
CLOSED_END_FUNDS = set("""
    ASA BFK BFZ BGR BIT BLE BLW BME BMEZ BTT BTZ BUI BYM CET CPZ ECC EFR EFT EIC ERC
    EVF EVN GAM GBAB HQH HQL IIM IQI KTF LEO MAV MCR MMT NBH NXDT NXP OCCI OXLC
    PMM PMO PPT RA RMT RVT STEW TY TYG VCV VGM VKI VKQ VMO ZF""".split())
UNITS_NOTES_SPACS = {"GIG-U", "GIX-U", "LHC-UN", "RCF-UN",  # SPAC units
                     "MOSC",  # SPAC shares
                     "SCCC",  # Sachem Capital 7.75% notes
                     "GYC"}  # Cabco trust certificates (floating-rate debt)
# Units, warrants, rights and preferreds by their code suffix (GIX-UN, LHC-WS,
# FITB-P-I, ISF-CL); share classes (BF-B, HEI-A) don't match.
NOT_SHARES_SUFFIX = re.compile(r"-(U|UN|W|WS|WT|RT|P-[A-Z]+|CL)$")
# Matches the suffix but is the company: EODHD files old Hertz's bars (2015
# to its 2020 bankruptcy) under HTZ-R-W; HTZ_old and HTZGQ have none.
SUFFIX_EXCEPTIONS = {"HTZ-R-W"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    force=True)
log = logging.getLogger("jml_screen")

_DDL = [
    "CREATE TABLE IF NOT EXISTS sessions (date TEXT PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS symbols (
        code TEXT NOT NULL, listing TEXT NOT NULL CHECK (listing IN ('current', 'delisted')),
        name TEXT, exchange TEXT, type TEXT, isin TEXT, PRIMARY KEY (code, listing))""",
    """CREATE TABLE IF NOT EXISTS bulk_eod (
        date TEXT NOT NULL, code TEXT NOT NULL, op REAL, hi REAL, lo REAL, cl REAL,
        ac REAL, vo REAL, PRIMARY KEY (date, code)) WITHOUT ROWID""",
    "CREATE TABLE IF NOT EXISTS bulk_dates (date TEXT PRIMARY KEY, rows INTEGER NOT NULL)",
]


def five_years_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 5)
    except ValueError:  # 29 February
        return d.replace(year=d.year - 5, day=28)


def screen_dates(sessions: list[date], start: date) -> list[date]:
    """The SCREEN_SESSIONS sessions before `start`; the last is the screen date."""
    before = [s for s in sessions if s < start]
    return before[-SCREEN_SESSIONS:]


def five_year_dates(sessions: list[date], start: date) -> list[date]:
    """The FIVE_YEAR_SESSIONS sessions on or before 5 years before `start`."""
    cut = five_years_before(start)
    return [s for s in sessions if s <= cut][-FIVE_YEAR_SESSIONS:]


def venue(code, exchange, isin, name) -> str:
    """'listed', 'inferred' (OTC now, an American company) or 'foreign_otc'.
    An OTC ADR has an American ISIN too, so it's caught by its code (four
    letters plus Y or F, the OTC convention) or its name."""
    if exchange in LISTED:
        return "listed"
    adr = bool(re.fullmatch(r"[A-Z]{4}[YF]", code)) or "ADR" in str(name)
    if isinstance(isin, str) and isin.startswith("US") and not adr:
        return "inferred"
    return "foreign_otc"


def not_shares_by_code(code: str) -> bool:
    return code not in SUFFIX_EXCEPTIONS and bool(NOT_SHARES_SUFFIX.search(code))


def band(turnover: float):
    for name, low in BANDS:
        if turnover >= low:
            return name
    return None


def median_turnover(rows: pd.DataFrame, dates: list[date]) -> pd.Series:
    """Per code, the median of cl x vo over `dates`, a day with no bar
    counting as 0 (it didn't trade)."""
    t = rows.assign(to=rows["cl"] * rows["vo"]).pivot_table(
        index="code", columns="date", values="to", aggfunc="first")
    t = t.reindex(columns=[d.isoformat() for d in dates]).fillna(0.0)
    return t.median(axis=1)


def duplicate_groups(rows: pd.DataFrame, dates: list[date]) -> list[list[str]]:
    """One stock under two symbols (a rename EODHD serves under both): closes
    within DUP_CLOSE_TOL on at least DUP_MIN_SHARE of the dates either traded.
    Measured 2026-11-05 on period 4: ten rename pairs matched on all 60
    sessions; share classes at most 25% (GOOG/GOOGL). Volume doesn't
    separate them: EODHD's two copies of a rename can differ by half.
    """
    cols = [d.isoformat() for d in dates]
    c = rows.pivot_table(index="code", columns="date", values="cl",
                         aggfunc="first").reindex(columns=cols)
    parent = {code: code for code in c.index}

    def root(x):
        while parent[x] != x:
            x = parent[x]
        return x

    # candidates share a close (to the cent) on one of the last 5 dates; a
    # rename matches on nearly every date, so this misses none and is far quicker
    pairs = set()
    for col in cols[-5:]:
        day = c[col].dropna().round(2)
        for _, codes in day.groupby(day):
            codes = sorted(codes.index)
            pairs.update((a, b) for i, a in enumerate(codes) for b in codes[i + 1:])
    for a, b in sorted(pairs):
        close_ok = ((c.loc[a] - c.loc[b]).abs() <= DUP_CLOSE_TOL * c.loc[a]).sum()
        traded = (c.loc[a].notna() | c.loc[b].notna()).sum()  # sessions either one traded
        if close_ok >= DUP_MIN_SHARE * traded:
            parent[root(b)] = root(a)
    groups: dict[str, list[str]] = {}
    for code in c.index:
        groups.setdefault(root(code), []).append(code)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


class Eodhd:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str, **params) -> requests.Response:
        url = f"{EODHD_BASE}/{path}"
        for attempt in range(3):
            try:
                r = requests.get(url, params={"api_token": self.token, **params}, timeout=180)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                log.warning("%s: %s (attempt %d)", path, _redact_token(str(e)), attempt + 1)
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"{path} failed three times")

    def used(self) -> int:
        return self.get("user", fmt="json").json()["apiRequests"]


def all_sessions(db: sqlite3.Connection) -> list[date]:
    return [date.fromisoformat(d) for (d,) in db.execute("SELECT date FROM sessions ORDER BY 1")]


def fetch(db: sqlite3.Connection, api: Eodhd) -> None:
    if not db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
        r = api.get("eod/SPY.US", fmt="json", **{"from": "2014-01-01", "to": "2025-12-31"})
        db.executemany("INSERT INTO sessions VALUES (?)",
                       [(b["date"],) for b in r.json() if b["volume"]])
        db.commit()
    if not db.execute("SELECT 1 FROM symbols LIMIT 1").fetchone():
        for listing, extra in (("current", {}), ("delisted", {"delisted": 1})):
            rows = api.get("exchange-symbol-list/US", fmt="json", **extra).json()
            db.executemany("INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?, ?, ?)",
                           [(s["Code"], listing, s["Name"], s["Exchange"], s["Type"], s["Isin"])
                            for s in rows])
        db.commit()
    sessions = all_sessions(db)
    needed = sorted({d for _, start, _ in PERIODS
                     for d in screen_dates(sessions, start) + five_year_dates(sessions, start)})
    have = {d for (d,) in db.execute("SELECT date FROM bulk_dates")}
    todo = [d for d in needed if d.isoformat() not in have]
    log.info("%d bulk dates needed, %d to fetch", len(needed), len(todo))
    for i, d in enumerate(todo):
        if i % 20 == 0 and api.used() + 100 > QUOTA_STOP:
            log.warning("stopping: apiRequests near %d; rerun after UTC midnight", QUOTA_STOP)
            return
        r = api.get("eod-bulk-last-day/US", date=d.isoformat(), fmt="csv")
        df = pd.read_csv(io.StringIO(r.text), keep_default_na=False, na_values=[""])
        df = df[df["Date"] == d.isoformat()]  # a stale row is the ticker's last bar, not this day's
        db.executemany("INSERT OR REPLACE INTO bulk_eod VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       df[["Date", "Code", "Open", "High", "Low", "Close", "Adjusted_close",
                           "Volume"]].itertuples(index=False, name=None))
        db.execute("INSERT OR REPLACE INTO bulk_dates VALUES (?, ?)", (d.isoformat(), len(df)))
        db.commit()
        if (i + 1) % 25 == 0:
            log.info("%d/%d bulk dates", i + 1, len(todo))
    log.info("bulk fetch complete")


def symbol_info(db: sqlite3.Connection) -> pd.DataFrame:
    """One row per code: the current listing if there is one, else the
    delisted one; `both` marks a code in both lists (a reused symbol)."""
    s = pd.read_sql("SELECT * FROM symbols", db)
    listed = s.groupby("listing")["code"].apply(set)
    both = listed.get("current", set()) & listed.get("delisted", set())
    s = s.sort_values(["code", "listing"]).drop_duplicates("code", keep="first")  # 'current' first
    s["both"] = s["code"].isin(both)
    return s.set_index("code")


def optionable_by_period(alpha: sqlite3.Connection) -> dict[int, set[str]]:
    """Per period, the EODHD-form codes (no suffix) in the latest OT
    snapshot on or before its start. Only those snapshots are kept."""
    by_date: dict[str, set[str]] = {}
    bodies = [b for (b,) in alpha.execute(
        "SELECT r.body FROM raw_responses r JOIN windows w ON w.id = r.window_id "
        "WHERE w.job = 'optionable' AND w.capture = 'history' "
        "AND w.completed_at IS NOT NULL AND w.superseded = 0")]
    dates = sorted({row["date"] for b in bodies for row in json.loads(b)["data"]})
    snap = {p: max(d for d in dates if d <= start.isoformat()) for p, start, _ in PERIODS}
    keep = set(snap.values())
    for b in bodies:
        for row in json.loads(b)["data"]:
            if row["date"] in keep:
                by_date.setdefault(row["date"], set()).add(row["ticker"].upper().replace(".", "-"))
    return {p: by_date[d] for p, d in snap.items()}


def report(db: sqlite3.Connection, alpha: sqlite3.Connection) -> None:
    sessions = all_sessions(db)
    have = {d for (d,) in db.execute("SELECT date FROM bulk_dates")}
    syms = symbol_info(db)
    db.execute("DROP TABLE IF EXISTS eligible")
    db.execute("DROP TABLE IF EXISTS screen_dropped")
    db.execute("""CREATE TABLE eligible (period INTEGER, code TEXT, band TEXT,
        median_turnover REAL, close REAL, ot INTEGER, venue_inferred INTEGER,
        exchange TEXT, name TEXT, reused_symbol INTEGER, PRIMARY KEY (period, code))""")
    db.execute("""CREATE TABLE screen_dropped (period INTEGER, code TEXT, reason TEXT,
        detail TEXT, PRIMARY KEY (period, code))""")
    summary = []
    ot_by_period = optionable_by_period(alpha)
    for period, start, end in PERIODS:
        sdates, fdates = screen_dates(sessions, start), five_year_dates(sessions, start)
        missing = [d for d in sdates + fdates if d.isoformat() not in have]
        if missing:
            log.warning("period %d: %d bulk dates not fetched yet", period, len(missing))
            continue
        screen = sdates[-1].isoformat()
        q = ",".join("?" * len(sdates))
        on_screen = pd.read_sql("SELECT code, cl, vo FROM bulk_eod WHERE date = ? AND vo > 0",
                                db, params=[screen]).set_index("code")
        dropped = []
        dropped += [(c, "bad_data", None) for c in on_screen.index if c in BAD_DATA]
        dropped += [(c, "closed_end_fund", None) for c in on_screen.index
                    if c in CLOSED_END_FUNDS]
        dropped += [(c, "unit_note_spac", None) for c in on_screen.index
                    if c in UNITS_NOTES_SPACS or not_shares_by_code(c)]
        on_screen = on_screen.drop([c for c in on_screen.index
                                    if c in BAD_DATA | CLOSED_END_FUNDS | UNITS_NOTES_SPACS
                                    or not_shares_by_code(c)])
        info = syms.reindex(on_screen.index)
        unlisted = on_screen.index[info["type"].isna()]
        pref = info["name"].str.contains(r"\bPref", na=False)
        dropped += [(c, "preferred", info.at[c, "name"]) for c in on_screen.index[pref]]
        common = on_screen.index[((info["type"] == "Common Stock") & ~pref)
                                 | on_screen.index.isin(list(UNLISTED_COMMON))]
        dropped += [(c, "not_in_symbol_lists", None) for c in unlisted
                    if c not in UNLISTED_COMMON]
        cand = pd.DataFrame(index=common)
        cand["venue"] = [venue(c, syms.at[c, "exchange"], syms.at[c, "isin"], syms.at[c, "name"])
                         if c in syms.index else "listed" for c in common]
        dropped += [(c, "foreign_otc", syms.at[c, "exchange"])
                    for c in cand.index[cand["venue"] == "foreign_otc"]]
        cand = cand[cand["venue"] != "foreign_otc"]
        cand["close"] = on_screen.loc[cand.index, "cl"]
        dropped += [(c, "price_below_2", f"{p:.4f}")
                    for c, p in cand["close"].items() if p < MIN_PRICE]
        cand = cand[cand["close"] >= MIN_PRICE]
        # only the candidates' 60 sessions: the whole market's don't fit in mars's memory
        db.execute("DROP TABLE IF EXISTS temp.cand")
        db.execute("CREATE TEMP TABLE cand (code TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO temp.cand VALUES (?)", [(c,) for c in cand.index])
        rows = pd.read_sql(f"SELECT b.date, b.code, b.cl, b.vo FROM bulk_eod b "
                           f"JOIN temp.cand USING (code) WHERE b.date IN ({q})", db,
                           params=[d.isoformat() for d in sdates])
        cand["median_turnover"] = median_turnover(rows, sdates)
        cand = cand[cand["median_turnover"] >= MIN_TURNOVER]
        five = {c for (c,) in db.execute(
            "SELECT DISTINCT code FROM bulk_eod WHERE vo > 0 "
            f"AND date IN ({','.join('?' * len(fdates))})", [d.isoformat() for d in fdates])}
        dropped += [(c, "under_5_years", None) for c in cand.index if c not in five]
        cand = cand[cand.index.isin(five)]
        for group in duplicate_groups(rows[rows["code"].isin(cand.index)], sdates):
            current = [c for c in group if c in syms.index and syms.at[c, "listing"] == "current"]
            keep = (current or group)[0]
            dropped += [(c, "duplicate_of", keep) for c in group if c != keep]
            cand = cand.drop([c for c in group if c != keep])
        ot = ot_by_period[period]
        cand["ot"] = cand.index.isin(ot).astype(int)
        cand["band"] = cand["median_turnover"].map(band)
        db.executemany("INSERT INTO eligible VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (period, f"{c}.US", r.band, float(r.median_turnover), float(r.close), int(r.ot),
             int(r.venue == "inferred"),
             syms.at[c, "exchange"] if c in syms.index else None,
             syms.at[c, "name"] if c in syms.index else UNLISTED_COMMON[c],
             int(syms.at[c, "both"]) if c in syms.index else 0) for c, r in cand.iterrows()])
        db.executemany("INSERT INTO screen_dropped VALUES (?, ?, ?, ?)",
                       [(period, f"{c}.US", reason, detail) for c, reason, detail in dropped])
        traded_codes = set(on_screen.index)
        summary.append({
            "period": period, "screen_date": screen, "traded": len(on_screen),
            **{b: int((cand["band"] == b).sum()) for b, _ in BANDS},
            "ot_eligible": int(cand["ot"].sum()),
            "inferred": int((cand["venue"] == "inferred").sum()),
            "foreign_otc": sum(r == "foreign_otc" for _, r, _ in dropped),
            "under_5y": sum(r == "under_5_years" for _, r, _ in dropped),
            "dupes": sum(r == "duplicate_of" for _, r, _ in dropped),
            "unlisted": sum(r == "not_in_symbol_lists" for _, r, _ in dropped),
            "ot_snapshot": len(ot), "ot_not_in_bulk": len(ot - traded_codes),
        })
    db.commit()
    print(pd.DataFrame(summary).to_string(index=False))


def hold_out_pool(codes: list[str]) -> set[str]:
    """A seeded HOLD_OUT_SHARE of all eligible stocks, across every period.
    The hold-out is drawn per stock, not per slot, so no stock is in-sample
    in one period and hold-out in another (their 5-year windows overlap)."""
    codes = sorted(set(codes))
    order = np.random.default_rng([SEED, 0]).permutation(len(codes))
    return {codes[i] for i in order[:round(HOLD_OUT_SHARE * len(codes))]}


def draw_band(codes: list[str], period: int, band_index: int, pool: int, n: int) -> list[str]:
    """n codes from a seeded shuffle of `codes` (sorted first)."""
    codes = sorted(codes)
    rng = np.random.default_rng([SEED, period, band_index, pool])
    return [codes[i] for i in rng.permutation(len(codes))[:n]]


def draw(db: sqlite3.Connection) -> None:
    if db.execute("SELECT name FROM sqlite_master WHERE name = 'draw'").fetchone():
        raise SystemExit("draw already exists: the sample is fixed (drop the table by hand "
                         "only if Ian asks for a new draw, and log it as a new experiment)")
    db.execute("""CREATE TABLE draw (period INTEGER, code TEXT, band TEXT,
        sample TEXT CHECK (sample IN ('in_sample', 'hold_out')), draw_rank INTEGER,
        inclusion_prob REAL, eligible_in_pool INTEGER, seed INTEGER, drawn_at TEXT,
        PRIMARY KEY (period, code))""")
    db.execute("DROP TABLE IF EXISTS stock_pool")
    db.execute("CREATE TABLE stock_pool (code TEXT PRIMARY KEY, sample TEXT NOT NULL)")
    every = [c for (c,) in db.execute("SELECT DISTINCT code FROM eligible")]
    hold = hold_out_pool(every)
    db.executemany("INSERT INTO stock_pool VALUES (?, ?)",
                   [(c, "hold_out" if c in hold else "in_sample") for c in sorted(set(every))])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for period, _, _ in PERIODS:
        for band_index, (name, sizes) in enumerate(DRAW_SIZES.items()):
            codes = [c for (c,) in db.execute(
                "SELECT code FROM eligible WHERE period = ? AND band = ?", (period, name))]
            for pool, (sample, n) in enumerate(zip(("in_sample", "hold_out"), sizes)):
                pool_codes = [c for c in codes if (c in hold) == (sample == "hold_out")]
                if len(pool_codes) < n:
                    raise SystemExit(f"period {period} {name} {sample}: only {len(pool_codes)}")
                picked = draw_band(pool_codes, period, band_index, pool, n)
                db.executemany("INSERT INTO draw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                    (period, c, name, sample, rank, n / len(pool_codes), len(pool_codes),
                     SEED, now) for rank, c in enumerate(picked, 1)])
    db.commit()
    print(pd.read_sql("SELECT sample, band, COUNT(*) n, ROUND(MIN(inclusion_prob), 4) p_min, "
                      "ROUND(MAX(inclusion_prob), 4) p_max FROM draw GROUP BY 1, 2", db)
          .to_string(index=False))


def stock_period_span(start: date, end: date) -> tuple[date, date]:
    """5 years before the period to a year after it, with a margin."""
    one_after = end.replace(year=end.year + 1)
    return (five_years_before(start) - timedelta(days=FETCH_MARGIN_DAYS),
            one_after + timedelta(days=FETCH_MARGIN_DAYS))


def fetch_drawn_daily(db: sqlite3.Connection, api: "Eodhd") -> None:
    """Daily bars for every drawn stock (in-sample and hold-out) over its
    stock-period span, plus SPY, into `daily`; each result in
    `fetch_status`. Resumable. One call per stock-period."""
    db.execute("""CREATE TABLE IF NOT EXISTS fetch_status (period INTEGER, code TEXT,
        from_date TEXT, to_date TEXT, status TEXT CHECK (status IN ('ok', 'error')),
        rows INTEGER, first_date TEXT, last_date TEXT, error TEXT, fetched_at TEXT,
        PRIMARY KEY (period, code))""")
    spans = {p: stock_period_span(start, end) for p, start, end in PERIODS}
    todo = [(p, c) for p, c in db.execute("SELECT period, code FROM draw ORDER BY period, code")]
    todo += [(0, "SPY.US")]  # period 0: the benchmark, over every period's span
    done = set(db.execute("SELECT period, code FROM fetch_status WHERE status = 'ok'"))
    todo = [t for t in todo if t not in done]
    log.info("%d stock-periods to fetch", len(todo))
    for i, (period, code) in enumerate(todo, 1):
        lo, hi = spans[period] if period else (min(s[0] for s in spans.values()),
                                               max(s[1] for s in spans.values()))
        rows = first = last = error = None
        try:
            pdf = fetch_daily(code, api.token, from_date=lo, to_date=hi)
            pandas2sqlite(pdf, db, "daily")
            real = pdf[pdf["vo"] > 0]
            rows, status = len(pdf), "ok"
            if not real.empty:
                first, last = str(real["date"].min()), str(real["date"].max())
        except Exception as e:  # recorded, never dropped: the extract lists it
            status, error = "error", _redact_token(f"{type(e).__name__}: {e}")[:500]
            log.warning("%s period %d: %s", code, period, error)
        db.execute("INSERT OR REPLACE INTO fetch_status VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (period, code, lo.isoformat(), hi.isoformat(), status, rows, first, last,
                    error, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        db.commit()
        if i % 100 == 0:
            log.info("%d/%d", i, len(todo))
        time.sleep(0.05)
    short = db.execute(
        "SELECT COUNT(*) FROM fetch_status JOIN draw USING (period, code) "
        "WHERE status = 'error' OR last_date < date(to_date, ?)",
        (f"-{FETCH_MARGIN_DAYS + 7} days",)).fetchone()[0]
    log.info("daily fetch complete; %d stock-periods stop early or failed (to research)", short)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["fetch", "report", "draw", "daily"])
    p.add_argument("--db-path", required=True, type=Path)
    p.add_argument("--alphanume-db", type=Path, default=Path.home() / "data" / "alphanume.db")
    args = p.parse_args()
    db = sqlite3.connect(args.db_path)
    for ddl in _DDL:
        db.execute(ddl)
    if args.action in ("fetch", "daily"):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        api = Eodhd(os.environ["EODHD_API_TOKEN"])
        (fetch if args.action == "fetch" else fetch_drawn_daily)(db, api)
    elif args.action == "draw":
        draw(db)
    else:
        alpha = sqlite3.connect(f"file:{args.alphanume_db}?mode=ro", uri=True)
        report(db, alpha)


if __name__ == "__main__":
    main()
