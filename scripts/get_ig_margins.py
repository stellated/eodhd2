#!/usr/bin/env python3
"""Monthly job: download IG's tiered margin spreadsheet and load a cleaned
AU/US-only subset into the `ig_margins` table.

Meant to run against a scratch copy of the database that a wrapper script
syncs down from remote storage before this runs and back up afterwards --
see ops/run_ig_margins.sh. It never decides where the canonical database
lives; it just reads/writes whatever --db-path points at. ops/run_ig_margins.sh
is also responsible for archiving the raw downloaded spreadsheet (every
country, not just AU/US) to remote storage, for continuity with its
predecessor's monthly archive on the old "nebula" machine -- this script
only downloads it locally and parses it.

See doc/DESIGN_DECISIONS.md ("IG tiered margins") for the full design:
- Caret-suffixed Reuters codes (e.g. "APT.AX^B22") mark a RIC Reuters has
  retired (delisted/merged/expired) -- confirmed against several real
  corporate actions. Dropped entirely rather than just stripped, since a
  reused ticker otherwise collides with the live RIC under the same code.
- 14 rows carry an identical sentinel tier1_range magnitude across
  unrelated tickers (not a genuine per-security overflow) -- tier1_limit/
  tier1_rate are set to NULL for these rather than "corrected".
- ig_margins keeps history (PK (code, download_date)), not just the
  latest snapshot. download_date is when we fetched the file;
  source_modified is when IG last changed it (the server's Last-Modified,
  kept by curl -R). The old marketdatasystems.com copy stopped changing
  on 2024-04-10; IG's own copy (TIERED_URL since 2026-10-23) is current.
- ig_margins_resolved (a view) also finds a stock tipped under a new
  symbol that IG's file still lists under its old one, via ticker_aliases.
"""
import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from subprocess import run

import pandas as pd
from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from eodhd_io import Database  # noqa: E402

# Linked from IG's "Shares and ETFs CFD product details" help article. IG's
# CDN refuses (403) a request without a browser User-Agent.
TIERED_URL = ("https://www.ig.com/content/dam/publicsites/go-anywhere-files/"
              "Tiered%20Margin%20Australia%20%28Retail%29.xlsx")
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TABLENAME = "ig_margins"
COUNTRY_SUFFIX = {"AUSTRALIA": "AU", "UNITED STATES": "US"}

RAW_COLUMNS = [
    "reuters", "description", "country", "new_positions", "shortable",
    "guaranteed_stop_premium", "borrow_rate", "tier1_range", "tier1_rate",
]

OUTPUT_COLUMNS = [
    "download_date", "code", "country", "reuters", "description",
    "new_positions", "shortable", "guaranteed_stop_premium", "borrow_rate",
    "tier1_limit", "tier1_rate", "source_modified",
]

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLENAME} (
    download_date TEXT NOT NULL,
    code TEXT NOT NULL,
    country TEXT,
    reuters TEXT,
    description TEXT,
    new_positions TEXT,
    shortable TEXT,
    guaranteed_stop_premium TEXT,
    borrow_rate TEXT,
    tier1_limit INTEGER,
    tier1_rate REAL,              -- percent; IG uses e.g. 30.8
    source_modified TEXT,         -- when IG last changed the file
    PRIMARY KEY (code, download_date)
)
"""

# Lookup by the symbol our tables use, per snapshot, via ticker_aliases.
# What IG's file lists depends on whether it predates a rename
# (source_modified < effective_date):
# - predates it: IG still has the old symbol, so the new symbol finds the
#   old-symbol row ("back"; e.g. DMRA -> GLTO in the 2024 file);
# - postdates it: IG has the new symbol, so the old symbol finds the
#   new-symbol row ("forward"; e.g. SATS -> ECHO), and IG's row under the
#   old symbol is dropped -- it's another or a reused listing (SATS is
#   "Strive Inc 15 PERP" in the 2026-10-23 file). A cash-out alias also
#   drops the old-symbol row, with nothing in its place.
# A row IG lists under the symbol itself wins over either. ig_code is the
# symbol IG used; renamed_on the alias date (NULL for a direct row). Values
# are IG's own: tier1_limit is a share count for ig_code, not rescaled by
# an alias ratio.
RESOLVED_VIEW = "ig_margins_resolved"
_IG_COLS = ", ".join(f"m.{c}" for c in OUTPUT_COLUMNS if c != "code")
RESOLVED_VIEW_DDL = f"""
CREATE VIEW {RESOLVED_VIEW} AS
WITH RECURSIVE
later(code, ig_code, renamed_on, depth) AS (     -- new symbol -> older ones
    SELECT new_code, old_code, effective_date, 1
    FROM ticker_aliases WHERE new_code IS NOT NULL
    UNION
    SELECT a.new_code, l.ig_code, l.renamed_on, l.depth + 1
    FROM later l JOIN ticker_aliases a ON a.old_code = l.code
    WHERE a.new_code IS NOT NULL AND l.depth < 10
),
earlier(code, ig_code, renamed_on, depth) AS (   -- old symbol -> newer ones
    SELECT old_code, new_code, effective_date, 1
    FROM ticker_aliases WHERE new_code IS NOT NULL
    UNION
    SELECT e.code, a.new_code, MAX(e.renamed_on, a.effective_date), e.depth + 1
    FROM earlier e JOIN ticker_aliases a ON a.old_code = e.ig_code
    WHERE a.new_code IS NOT NULL AND e.depth < 10
),
snaps AS (SELECT DISTINCT download_date, source_modified FROM {TABLENAME}),
gone AS (                                        -- symbol renamed/cashed out
    SELECT DISTINCT a.old_code AS code, s.download_date
    FROM ticker_aliases a JOIN snaps s ON a.effective_date <= s.source_modified
),
candidates(code, ig_code, renamed_on, download_date, priority) AS (
    SELECT m.code, m.code, NULL, m.download_date, 0
    FROM {TABLENAME} m
    WHERE NOT EXISTS (SELECT 1 FROM gone g
                      WHERE g.code = m.code AND g.download_date = m.download_date)
    UNION ALL
    SELECT l.code, l.ig_code, l.renamed_on, s.download_date, l.depth
    FROM later l JOIN snaps s ON s.source_modified < l.renamed_on
    UNION ALL
    SELECT e.code, e.ig_code, e.renamed_on, s.download_date, 100 - e.depth
    FROM earlier e JOIN snaps s ON e.renamed_on <= s.source_modified
),
ranked AS (
    SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.code, c.download_date
                                   ORDER BY c.priority) AS rn
    FROM candidates c JOIN {TABLENAME} m
      ON m.code = c.ig_code AND m.download_date = c.download_date
    WHERE c.priority = 0 OR NOT EXISTS (SELECT 1 FROM gone g
                    WHERE g.code = c.ig_code AND g.download_date = c.download_date)
)
SELECT r.code, r.ig_code, r.renamed_on, {_IG_COLS}
FROM ranked r JOIN {TABLENAME} m
  ON m.code = r.ig_code AND m.download_date = r.download_date
WHERE r.rn = 1
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("get_ig_margins")


def download_spreadsheet(out_path: Path) -> None:
    """curl the spreadsheet to out_path. Skips if out_path already exists
    (download_date is derived from the run date, not the file's own
    metadata, so a retried run on the same day is naturally idempotent)."""
    if out_path.exists():
        log.info("already have %s, not re-downloading", out_path)
        return
    log.info("downloading tiered margin spreadsheet to %s", out_path)
    # -R keeps the server's Last-Modified as the file's mtime (source_modified);
    # -f fails on an HTTP error instead of saving the error page as the file
    run(["curl", "-fsSLR", "-A", USER_AGENT, TIERED_URL, "--output", str(out_path)],
        check=True)


def source_modified(xlsx_path: Path) -> date:
    return datetime.fromtimestamp(xlsx_path.stat().st_mtime, timezone.utc).date()


def parse_tiered_spreadsheet(xlsx_path: Path, download_date: date,
                             modified: date | None = None) -> pd.DataFrame:
    """Parse the raw spreadsheet into the AU/US-only, cleaned shape that
    goes into ig_margins. Columns after the first 9 are IG's own further
    tiers (tier2, tier3, ...) -- out of scope, this table is deliberately
    "just the basic tiered margin info" (tier 1 only)."""
    raw = pd.read_excel(xlsx_path, header=4).iloc[:, :9]
    raw.columns = RAW_COLUMNS

    df = raw[raw["country"].isin(COUNTRY_SUFFIX)].copy()
    df = df[~df["guaranteed_stop_premium"].str.contains("ERROR", na=False)]

    # See module docstring / doc/DESIGN_DECISIONS.md -- a caret marks a RIC
    # Reuters has retired; dropping these resolves every (code) collision
    # a reused ticker would otherwise create.
    df = df[~df["reuters"].str.contains(r"\^", na=False)]

    root = df["reuters"].str.split(".", n=1).str[0]
    df["code"] = root + "." + df["country"].map(COUNTRY_SUFFIX)

    # Two RICs can share a root (WBCPJ.AX, a Westpac preference share, and
    # WBCPJ.AX1, a Westpac capital note, in the 2026-10-23 file): keep the
    # one whose exchange suffix has no digit, and log the other.
    numbered = df["reuters"].str.split(".", n=1).str[1].str.contains(r"\d", na=False)
    df = df.assign(_numbered=numbered).sort_values("_numbered", kind="stable")
    dup = df["code"].duplicated()
    if dup.any():
        log.warning("dropped %d row(s) sharing a code with another RIC: %s", dup.sum(),
                    ", ".join(df.loc[dup, "reuters"]))
    df = df[~dup].drop(columns="_numbered").sort_index()

    # IG's feed emits this exact sentinel magnitude -- confirmed identical
    # across 13 unrelated tickers with very different borrow rates, not a
    # genuine per-security overflow -- when it has no real tier1 boundary
    # to report. Treated as missing, not "corrected" by flipping the sign.
    is_sentinel = df["tier1_range"].str.startswith("0 - -", na=False)
    if is_sentinel.any():
        log.warning(
            "%d row(s) have IG's known sentinel tier1_range value -- "
            "tier1_limit/tier1_rate set to NULL, not corrected (see "
            "doc/DESIGN_DECISIONS.md, 'IG tiered margins'): %s",
            is_sentinel.sum(),
            ", ".join(df.loc[is_sentinel, "code"]),
        )

    tier1_limit_text = df["tier1_range"].str.split(" - ", n=1).str[1].str.replace(",", "")
    df["tier1_limit"] = pd.to_numeric(tier1_limit_text, errors="coerce").astype("Int64")
    df["tier1_rate"] = pd.to_numeric(
        df["tier1_rate"].str.replace("%", ""), errors="coerce"
    ).astype("Float64")
    df.loc[is_sentinel, ["tier1_limit", "tier1_rate"]] = pd.NA

    df["download_date"] = download_date.isoformat()
    df["source_modified"] = modified.isoformat() if modified else None

    return df[OUTPUT_COLUMNS].reset_index(drop=True)


def write_to_sqlite(df: pd.DataFrame, db: Database, tablename: str = TABLENAME) -> None:
    """INSERT OR REPLACE on (code, download_date) -- idempotent for a
    same-day retry, and append-only month to month (ig_margins keeps
    history, unlike most of this project's other tables)."""
    db.conn.execute(DDL)
    create_resolved_view(db.conn)
    clean = df.astype(object).where(df.notna(), None)
    cols = list(clean.columns)
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    rows = [tuple(r) for r in clean[cols].itertuples(index=False, name=None)]
    db.conn.executemany(
        f"INSERT OR REPLACE INTO {tablename} ({col_list}) VALUES ({placeholders})",  # noqa: S608
        rows,
    )
    db.conn.commit()


def create_resolved_view(conn) -> None:
    """Add source_modified to an ig_margins created before it existed, and
    (re)create ig_margins_resolved. Needs ticker_aliases to query the view."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLENAME})")}
    if "source_modified" not in cols:
        conn.execute(f"ALTER TABLE {TABLENAME} ADD COLUMN source_modified TEXT")
    conn.execute(f"DROP VIEW IF EXISTS {RESOLVED_VIEW}")
    conn.execute(RESOLVED_VIEW_DDL)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument(
        "--xlsx-dir", required=True, type=Path,
        help="Where this run's downloaded spreadsheet is saved -- the "
             "wrapper script is responsible for archiving it remotely.",
    )
    args = parser.parse_args()

    load_dotenv()
    args.xlsx_dir.mkdir(parents=True, exist_ok=True)

    today = date.today()
    xlsx_path = args.xlsx_dir / f"tiered.{today.isoformat()}.xlsx"
    download_spreadsheet(xlsx_path)

    with Database(args.db_path) as db:
        margins = parse_tiered_spreadsheet(xlsx_path, today, source_modified(xlsx_path))
        write_to_sqlite(margins, db)
        log.info("wrote %d AU/US margin row(s) for %s into %s", len(margins), today, TABLENAME)


if __name__ == "__main__":
    main()
