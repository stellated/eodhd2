#!/usr/bin/env python3
"""Daily capture of which stocks have listed options, US and AU, into
optionable.db: a free, ongoing replacement for Alphanume's Optionable
Tickers snapshots, whose captured history ends at 2026-10-01.

Sources (no history is served by any of them, so a day not captured is
lost; hence daily, not monthly):
- US: OCC's list of option underlyings (all US exchanges), Cboe's symbol
  directory, and Cboe's list of available weeklys.
- AU: ASX's derivatives master list, every option series listed on ASX.
- EODHD's exchange symbol lists (US, AU; 1 call each), to classify each
  underlying the same day (Common Stock / ETF / ..., exchange, name).

Every download is checked for its expected header, gzipped unchanged into
--archive-dir/<capture_date>/, and recorded in `captures`. Tables:
- us_symbols: every symbol OCC or Cboe lists, with the EODHD match and
  `eligible`, the Alphanume-like rule: Common Stock on NYSE, NASDAQ or
  AMEX (EODHD's classification), not an ADR (by name), listed by OCC.
  Matched to Alphanume's 2026-10-01 snapshot (checked 2026-11-25), it
  kept 3,522 symbols against 3,533, 3,480 in both, about what Alphanume's
  own list changed over the two months between.
- au_underlyings: per underlying and product type (OS = options over
  stock, LE = low exercise price options, OI = index), series and
  expiry counts, `has_weeklies`, `avg_days_between`. `eligible` = OS over
  an EODHD Common Stock. An adjusted class after a corporate action (e.g.
  GMDN beside GMD) maps to its stock with `adjusted_class` = 1 and isn't
  eligible itself.
- optionable (view): the eligible rows of both, in Alphanume's shape
  (ticker, has_weeklies, avg_days_between; the latter NULL for US).

A rerun on the same capture_date replaces that date's rows.

  get_optionable.py --db-path ~/data/optionable.db \\
      --archive-dir ~/data/optionable_raw [--date YYYY-MM-DD]
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from eodhd_io import _redact_token  # noqa: E402

log = logging.getLogger("optionable")

UA = "Mozilla/5.0 (X11; Linux x86_64) eodhd2-research"
SOURCES = {
    # name: (market, url, archive extension, text that must start the file)
    "occ": ("US", "https://marketdata.theocc.com/delo-download"
            "?prodType=ALL&downloadFields=OS;US;SN&format=txt", "txt", None),
    "cboe_dir": ("US", "https://www.cboe.com/us/options/symboldir/"
                 "equity_index_options/?download=csv", "csv", "Company Name,"),
    "cboe_weeklys": ("US", "https://www.cboe.com/available_weeklys/get_csv_download/",
                     "csv", "List of Available Weekly Options"),
    "asx_master": ("AU", "https://www2.asx.com.au/data/data/ASXCLDerivativesMasterList.csv",
                   "csv", '"BusDate","Market","ASXCode","Underlying"'),
}
EODHD_LISTS = {"eodhd_us": "US", "eodhd_au": "AU"}
MIN_ROWS = {"occ": 3000, "cboe_dir": 2000, "cboe_weeklys": 100, "asx_master": 10000,
            "eodhd_us": 20000, "eodhd_au": 1500}

US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "NYSE MKT"}
ADR_RE = re.compile(r"\bADRS?\b|\bADS\b|DEPOSITARY|DEPOSITORY|SPONSORED|"
                    r"NEW YORK REGISTRY|NY REGISTRY", re.I)
TICKER_RE = re.compile(r"[A-Z][A-Z0-9./]{0,6}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    capture_date TEXT NOT NULL, source TEXT NOT NULL, market TEXT NOT NULL,
    url TEXT, fetched_at TEXT, bytes INTEGER, sha1 TEXT, rows INTEGER,
    as_of TEXT, archive_path TEXT, error TEXT,
    PRIMARY KEY (capture_date, source));
CREATE TABLE IF NOT EXISTS us_symbols (
    capture_date TEXT NOT NULL, symbol TEXT NOT NULL,
    in_occ INTEGER NOT NULL, in_cboe INTEGER NOT NULL, has_weeklies INTEGER NOT NULL,
    occ_name TEXT, cboe_name TEXT,
    code TEXT, eodhd_type TEXT, eodhd_exchange TEXT, eodhd_name TEXT,
    adr INTEGER NOT NULL, eligible INTEGER NOT NULL,
    PRIMARY KEY (capture_date, symbol));
CREATE TABLE IF NOT EXISTS au_underlyings (
    capture_date TEXT NOT NULL, as_of TEXT, underlying TEXT NOT NULL,
    product_type TEXT NOT NULL, n_series INTEGER, n_expiries INTEGER,
    first_expiry TEXT, last_expiry TEXT, has_weeklies INTEGER,
    avg_days_between REAL,
    code TEXT, eodhd_type TEXT, eodhd_name TEXT,
    adjusted_class INTEGER NOT NULL, eligible INTEGER NOT NULL,
    PRIMARY KEY (capture_date, underlying, product_type));
DROP VIEW IF EXISTS optionable;
CREATE VIEW optionable AS
    SELECT capture_date, 'US' AS market, symbol AS ticker, code,
           has_weeklies, NULL AS avg_days_between
      FROM us_symbols WHERE eligible
    UNION ALL
    SELECT capture_date, 'AU', underlying, code, has_weeklies, avg_days_between
      FROM au_underlyings WHERE eligible;
"""


# ---------- parsing (pure; tested) ----------

def parse_occ(text: str) -> dict[str, str]:
    """OCC underlyings: {symbol: name}. Lines are `option symbol<TAB>
    underlying<TAB>name`; adjusted option symbols (a digit first, e.g.
    1AAPL) share the underlying of the standard one and are skipped."""
    out = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 3 or not p[0].strip() or p[0].strip()[0].isdigit():
            continue
        und = p[1].strip()
        if und:
            out.setdefault(und, p[2].strip())
    return out


def parse_cboe_dir(text: str) -> dict[str, str]:
    """Cboe symbol directory: {symbol: company name}."""
    out = {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) >= 2 and r[0] != "Company Name" and TICKER_RE.fullmatch(r[1].strip()):
            out[r[1].strip()] = r[0].strip()
    return out


def parse_cboe_weeklys(text: str) -> set[str]:
    """Cboe available weeklys: the symbols (first column). The first rows
    are expiry calendars ("Standard", "Expanded", ...), not symbols."""
    return {r[0].strip() for r in csv.reader(io.StringIO(text))
            if r and TICKER_RE.fullmatch(r[0].strip())}


def parse_asx_master(text: str) -> tuple[str | None, list[dict]]:
    """ASX derivatives master list: (BusDate as ISO, rows)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    bus = rows[0]["BusDate"] if rows else None
    if bus:
        bus = datetime.strptime(bus, "%d/%m/%Y").date().isoformat()
    return bus, rows


def norm(sym: str) -> str:
    """Class shares: OCC writes BRKB, Cboe BRK.B, EODHD BRK-B."""
    return sym.replace(".", "").replace("-", "").replace("/", "")


def build_us_rows(occ: dict, cboe: dict, weeklys: set, eodhd: list[dict]) -> list[dict]:
    exact = {s["Code"]: s for s in eodhd}
    by_norm = {}
    for s in eodhd:
        by_norm.setdefault(norm(s["Code"]), s)
    weekly_norm = {norm(w) for w in weeklys}
    rows = []
    for sym in sorted(set(occ) | set(cboe)):
        s = exact.get(sym) or by_norm.get(norm(sym))
        names = " ".join(filter(None, [occ.get(sym), cboe.get(sym), s and s.get("Name")]))
        adr = bool(ADR_RE.search(names))
        eligible = bool(sym in occ and s and s.get("Type") == "Common Stock"
                        and s.get("Exchange") in US_EXCHANGES and not adr)
        rows.append({
            "symbol": sym, "in_occ": int(sym in occ), "in_cboe": int(sym in cboe),
            "has_weeklies": int(norm(sym) in weekly_norm),
            "occ_name": occ.get(sym), "cboe_name": cboe.get(sym),
            "code": s["Code"] if s else None, "eodhd_type": s and s.get("Type"),
            "eodhd_exchange": s and s.get("Exchange"), "eodhd_name": s and s.get("Name"),
            "adr": int(adr), "eligible": int(eligible)})
    return rows


def build_au_rows(asx_rows: list[dict], eodhd: list[dict]) -> list[dict]:
    syms = {s["Code"]: s for s in eodhd}
    groups = defaultdict(list)
    for r in asx_rows:
        groups[(r["Underlying"], r["ProductType"])].append(r)
    os_unds = {u for u, pt in groups if pt == "OS"}
    out = []
    for (und, pt), rs in sorted(groups.items()):
        exps = sorted({datetime.strptime(r["ExpDate"], "%d/%m/%Y").date() for r in rs})
        gaps = [(b - a).days for a, b in zip(exps, exps[1:])]
        code, adjusted = und, 0
        if und not in syms and und[:-1] in syms and und[:-1] in os_unds:
            code, adjusted = und[:-1], 1   # e.g. GMDN: an adjusted GMD class
        s = syms.get(code)
        out.append({
            "underlying": und, "product_type": pt, "n_series": len(rs),
            "n_expiries": len(exps), "first_expiry": exps[0].isoformat(),
            "last_expiry": exps[-1].isoformat(),
            "has_weeklies": int(any(r["ListingFrequency"].startswith("W") for r in rs)),
            "avg_days_between": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "code": code if s else None, "eodhd_type": s and s.get("Type"),
            "eodhd_name": s and s.get("Name"), "adjusted_class": adjusted,
            "eligible": int(pt == "OS" and not adjusted and bool(s)
                            and s.get("Type") == "Common Stock")})
    return out


# ---------- fetching ----------

def fetch(url: str, params: dict | None = None) -> bytes:
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {_redact_token(r.url)}")
    return r.content


def check(name: str, body: bytes, n_rows: int) -> None:
    head = SOURCES.get(name, (None, None, None, None))[3]
    text = body[:200].decode("latin-1")
    if head and not text.startswith(head):
        raise RuntimeError(f"{name}: unexpected start {text[:80]!r}")
    if n_rows < MIN_ROWS[name]:
        raise RuntimeError(f"{name}: only {n_rows} rows (expected {MIN_ROWS[name]}+)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db-path", type=Path, required=True)
    ap.add_argument("--archive-dir", type=Path, required=True)
    ap.add_argument("--date", help="capture date (default: today in New York)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        force=True)
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ["EODHD_API_TOKEN"]
    cap = args.date or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    date.fromisoformat(cap)
    arch = args.archive_dir / cap
    arch.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(args.db_path)
    db.executescript(SCHEMA)
    got, failed = {}, []
    jobs = [(n, m, u, None, e) for n, (m, u, e, _) in SOURCES.items()]
    jobs += [(n, m, f"https://eodhd.com/api/exchange-symbol-list/{m}",
              {"api_token": token, "fmt": "json"}, "json") for n, m in EODHD_LISTS.items()]
    for name, market, url, params, ext in jobs:
        rec = {"capture_date": cap, "source": name, "market": market,
               "url": _redact_token(url), "fetched_at": datetime.now(timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%SZ"), "bytes": None, "sha1": None, "rows": None,
               "as_of": None, "archive_path": None, "error": None}
        try:
            body = fetch(url, params)
            rec["bytes"], rec["sha1"] = len(body), hashlib.sha1(body).hexdigest()
            path = arch / f"{name}.{ext}.gz"
            path.write_bytes(gzip.compress(body))
            rec["archive_path"] = str(path.relative_to(args.archive_dir))
            text = body.decode("utf-8-sig" if ext == "json" else "latin-1")
            if name == "occ":
                parsed = parse_occ(text)
            elif name == "cboe_dir":
                parsed = parse_cboe_dir(text)
            elif name == "cboe_weeklys":
                parsed = parse_cboe_weeklys(text)
            elif name == "asx_master":
                rec["as_of"], parsed = parse_asx_master(text)
            else:
                parsed = json.loads(text)
            rec["rows"] = len(parsed)
            check(name, body, len(parsed))
            got[name] = parsed
        except Exception as e:  # recorded, then the run exits non-zero
            rec["error"] = _redact_token(str(e))[:500]
            failed.append(name)
            log.error("%s: %s", name, rec["error"])
        db.execute(f"INSERT OR REPLACE INTO captures ({','.join(rec)}) "
                   f"VALUES ({','.join('?' * len(rec))})", list(rec.values()))
        log.info("%s: %s rows, %s bytes", name, rec["rows"], rec["bytes"])

    def store(table, rows, extra):
        db.execute(f"DELETE FROM {table} WHERE capture_date = ?", (cap,))
        for r in rows:
            r = {**extra, **r}
            db.execute(f"INSERT INTO {table} ({','.join(r)}) VALUES ({','.join('?' * len(r))})",
                       list(r.values()))

    # Each market is stored only if all of its sources arrived, so a day's
    # snapshot is never built from a partial set.
    if {"occ", "cboe_dir", "cboe_weeklys", "eodhd_us"} <= got.keys():
        rows = build_us_rows(got["occ"], got["cboe_dir"], got["cboe_weeklys"], got["eodhd_us"])
        store("us_symbols", rows, {"capture_date": cap})
        unmatched = [r["symbol"] for r in rows if r["in_occ"] and not r["code"]]
        log.info("US: %d symbols, %d eligible, %d weeklies; %d OCC symbols not in EODHD's list",
                 len(rows), sum(r["eligible"] for r in rows),
                 sum(r["has_weeklies"] and r["eligible"] for r in rows), len(unmatched))
    if {"asx_master", "eodhd_au"} <= got.keys():
        rows = build_au_rows(got["asx_master"], got["eodhd_au"])
        bus = db.execute("SELECT as_of FROM captures WHERE capture_date=? AND source='asx_master'",
                         (cap,)).fetchone()[0]
        store("au_underlyings", rows, {"capture_date": cap, "as_of": bus})
        unmatched = [r["underlying"] for r in rows if r["product_type"] == "OS" and not r["code"]]
        log.info("AU (ASX %s): %d OS underlyings, %d eligible, %d weeklies; unmatched OS: %s",
                 bus, sum(r["product_type"] == "OS" for r in rows),
                 sum(r["eligible"] for r in rows),
                 sum(r["eligible"] and r["has_weeklies"] for r in rows), unmatched or "none")
        if unmatched:
            log.warning("AU OS underlyings with no EODHD match: %s", unmatched)
    db.commit()
    db.close()
    if failed:
        log.error("failed sources: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
