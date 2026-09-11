""" tips_io.py ---------- Parsing and persistence helpers for StockDataAnalytics daily tip emails.
Parses .eml files into two pandas DataFrames:
    exchange_df -- one row per email with exchange-level summary data
    tips_df -- one row per tip (up to 20 per email)

Colour fields use an integer traffic-light scale:
    1 = green (#22c55e)
    2 = yellow (#eab308 / #ca8a04)
    3 = orange (#f97316)
    4 = red (#ef4444)

Each tip has four quality scores -- pattern_quality_score, setup_score,
risk_reward_score, context_score -- each with its own maximum (see
CATEGORY_MAX) and its own colour column. For the "full" cards (tip_n 1-3)
the score is the newsletter's own printed number, exact. For "compact"
cards (tip_n 4-20) the newsletter shows only a coloured bar, no number, so
the score is *estimated* from the bar's filled pixel height (see
_height_to_score). The estimate's precision is roughly +/-0.75-1.7 points
depending on category -- see doc/LIMITATIONS.md. Both cases fill in the same
column; nothing downstream needs to know which kind of card a row came from.

Public API ----------
    parse_tip_email(eml_path) -> (exchange_df, tips_df)
    parse_tip_emails([paths]) -> (exchange_df, tips_df) concatenated
    tips_exchange2sqlite(exc, tips, db, ...) -> None
    tips_sqlite2pandas(db, ..., start, end) -> (exchange_df, tips_df)

The tips() function (fetching OHLCV data around each tip date) lives in eodhd_io.py
and imports Database from this project. tips_io.py has no dependency on eodhd_io.py.
"""
from __future__ import annotations

import logging
import re
import pathlib
import sqlite3
from datetime import date, datetime
from typing import Optional, Union

import email as _email_module
from email import policy as _email_policy
from bs4 import BeautifulSoup
import pandas as pd
from eodhd_io import Database, is_connection_closed

# Set up logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------------------------------------------------------
# Colour mapping (hex -> integer)
# ---------------------------------------------------------------------------
# CHANGED: restored the 1-4 traffic-light scale (had drifted to a flat 1-7
# sequence, putting orange/red off by one -- see doc/CODE_REVIEW_2026-09-11.md
# section 1.1). Verified against every captured email under scripts/data/html/:
# the four per-tip score bars (pattern quality, setup, risk/reward, context)
# only ever use #22c55e/#eab308/#f97316 -- red is never observed there, but is
# kept as the natural top of the scale (it's used for the week/month % colour
# fields, which share this same 1-4 scheme). #ca8a04 is a darker amber variant
# used only for the regime-score text and aliases to yellow, per
# doc/TECHNICAL_SPEC.md. #854d0e (market-state badge) and #94a3b8 (generic
# label grey) were never actually reachable through _hex_to_int in this
# module's parsing code, so they're dropped rather than carried along.
_COLOUR_INT: dict[str, int] = {
    "#22c55e": 1,  # green
    "#eab308": 2,  # yellow
    "#ca8a04": 2,  # dark amber alias for yellow (regime score text)
    "#f97316": 3,  # orange
    "#ef4444": 4,  # red
}

def _hex_to_int(hex_colour: Optional[str]) -> Optional[int]:
    """Convert a CSS hex colour to a traffic-light integer (1-4), or None."""
    if not hex_colour:
        return None
    if hex_colour.lower() not in _COLOUR_INT:
        logging.warning(f"Unrecognized colour: {hex_colour}")
        return None
    return _COLOUR_INT[hex_colour.lower()]

def _extract_colour(style: str) -> Optional[int]:
    """Extract the colour integer from a CSS style string (uses color: property)."""
    m = re.search(r'\bcolor\s*:\s*(#\w{6})', style, re.IGNORECASE)
    return _hex_to_int(m.group(1)) if m else None

def _extract_bg_colour(style: str) -> Optional[int]:
    """Extract the colour integer from the background/background-colour property."""
    m = re.search(r'background(?:-color)?\s*:\s*(#\w{6})', style, re.IGNORECASE)
    return _hex_to_int(m.group(1)) if m else None

def _clean(text: str) -> str:
    return " ".join(text.split())

# ---------------------------------------------------------------------------
# Score-bar constants (empirically derived -- see doc/DESIGN_DECISIONS.md)
# ---------------------------------------------------------------------------
# Maximum possible value for each of the four per-tip quality scores.
# Confirmed by every "Total Score: X / 98" seen across all 166 captured
# emails (the denominator is 98 with zero exceptions), cross-checked by
# fitting height = floor(score * BOX_HEIGHT_FULL / max) against every
# (score, height) pair on the full-detail cards -- a unique exact fit for
# Setup (20) and Risk/Reward (18); Pattern Quality (40) and Context (20)
# resolved via the /98 total once Setup and Risk/Reward are pinned down.
CATEGORY_MAX: dict[str, float] = {
    "pattern_quality": 40,
    "setup": 20,
    "risk_reward": 18,
    "context": 20,
}

# Pixel height of the score bar's outer box in each card format. Constant
# across every category and every one of the 166 captured emails.
BOX_HEIGHT_FULL = 40     # tips 1-3 (full-detail cards: printed number + bar)
BOX_HEIGHT_COMPACT = 24  # tips 4-20 (compact cards: bar only, no number)

# Colour-bucket thresholds, expressed as a fraction of a category's max.
# Verified against all 13,040 score bars in the captured corpus with zero
# exceptions: green never below 0.75, yellow never below 0.50 or above
# ~0.71, orange never above 0.45. Red (#ef4444) was never observed on any
# of the four per-tip quality scores in the corpus -- it's used elsewhere
# (week/month % colour), so it is not derivable from a score here.
COLOUR_THRESHOLD_GREEN = 0.75
COLOUR_THRESHOLD_YELLOW = 0.50


def _height_to_score(height_filled: int, category: str, box_height: int) -> float:
    """Convert a score bar's filled pixel height to an estimated score.
    Rounded to 1 decimal place -- matches the precision the full-detail
    cards actually print for Risk/Reward and Context.
    """
    return round(height_filled * CATEGORY_MAX[category] / box_height, 1)


def _score_to_colour(score: float, category: str) -> int:
    """Return the traffic-light integer (1=green, 2=yellow, 3=orange) implied
    by a score, using the thresholds observed throughout the captured email
    corpus. Never returns 4 (red): no per-tip quality score in the observed
    data ever fell in a band low enough to justify it.
    """
    fraction = score / CATEGORY_MAX[category]
    if fraction >= COLOUR_THRESHOLD_GREEN:
        return 1
    if fraction >= COLOUR_THRESHOLD_YELLOW:
        return 2
    return 3


def _check_score_bounds(score: Optional[float], category: str, card_desc: str) -> None:
    """Log (not raise) if a parsed/estimated score falls outside [0, max] --
    would indicate the newsletter's scoring rubric has changed."""
    if score is None:
        return
    category_max = CATEGORY_MAX[category]
    if not (0 <= score <= category_max):
        logging.warning(
            f"{card_desc}: {category} score {score} is outside the expected "
            f"[0, {category_max}] range -- newsletter format may have changed."
        )


def _check_colour_consistency(
    score: Optional[float], parsed_colour: Optional[int], category: str, card_desc: str
) -> None:
    """Log (not raise) if the colour parsed from the HTML disagrees with the
    colour implied by the score under the 75%/50% threshold rule. For full
    cards this is an independent cross-check (score and colour come from
    separate HTML elements); for compact cards it's closer to a
    self-consistency check, since both the estimated score and the parsed
    colour derive from the same pixel height.
    """
    if score is None or parsed_colour is None:
        return
    derived = _score_to_colour(score, category)
    if derived != parsed_colour:
        logging.warning(
            f"{card_desc}: {category} parsed colour {parsed_colour} does not "
            f"match colour {derived} derived from score {score} "
            f"(max {CATEGORY_MAX[category]}) -- check for a newsletter format change."
        )


def _check_total_score(card_td, scores: dict, card_desc: str) -> None:
    """Cross-check a full-detail card's own 'Total Score: X / Y' text against
    the four parsed category scores. Not persisted -- purely a validation
    signal that the newsletter's scoring rubric hasn't silently changed.
    """
    m = re.search(r'Total Score:\s*([\d.]+)\s*/\s*([\d.]+)', card_td.get_text())
    if not m:
        return
    displayed_total, displayed_max = float(m.group(1)), float(m.group(2))
    expected_max = sum(CATEGORY_MAX.values())
    if displayed_max != expected_max:
        logging.warning(
            f"{card_desc}: Total Score denominator {displayed_max} != expected "
            f"{expected_max} (sum of CATEGORY_MAX) -- newsletter scoring rubric "
            f"may have changed."
        )
    if all(v is not None for v in scores.values()):
        actual_total = sum(scores.values())
        if abs(actual_total - displayed_total) > 0.01:
            logging.warning(
                f"{card_desc}: sum of 4 category scores ({actual_total}) != "
                f"displayed Total Score ({displayed_total})."
            )

# ---------------------------------------------------------------------------
# HTML extraction from .eml
# ---------------------------------------------------------------------------
def _get_html(eml_path: pathlib.Path) -> bytes:
    """Extract the HTML body bytes from an .eml file."""
    with open(eml_path, "rb") as f:
        msg = _email_module.message_from_binary_file(f, policy=_email_policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True)
    raise ValueError(f"No text/html part found in {eml_path}")

# ---------------------------------------------------------------------------
# Exchange summary parsing
# ---------------------------------------------------------------------------
def _parse_exchange(soup: BeautifulSoup, eml_path: pathlib.Path) -> dict:
    """Parse the exchange-level summary from the soup of one email."""
    # Exchange from <title>: "[NASDAQ] Stock Data Analytics..."
    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else ""
    exchange_m = re.search(r'\[(NASDAQ|NYSE)\]', title_text)
    exchange = exchange_m.group(1) if exchange_m else None

    # Tip date from header paragraph "April 08, 2026"
    date_tag = soup.find("p", string=re.compile(
        r'(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},\s+\d{4}'
    ))
    tip_date = None
    if date_tag:
        try:
            tip_date = datetime.strptime(
                _clean(date_tag.get_text()), "%B %d, %Y"
            ).date()
        except ValueError:
            pass

    # Market state: the rounded badge span (e.g. "Weak Bear")
    market_state = None
    for span in soup.find_all("span", style=True):
        style = span.get("style", "")
        if "border-radius: 20px" in style and "font-weight: 600" in style:
            txt = _clean(span.get_text())
            if txt and txt not in ("Premium Tier",):
                market_state = txt
                break

    # Week percent, month percent, regime score — all in font-size: 16px spans
    week_pct = month_pct = vol_str = regime_score = None
    week_colour = month_colour = regime_colour = None
    spans_16 = [s for s in soup.find_all("span", style=True) if "16px" in s.get("style", "")]
    for s in spans_16:
        txt = _clean(s.get_text())
        style = s.get("style", "")
        if re.match(r'^[+\-]?\d+\.?\d*%$', txt):
            val = float(txt.replace('%', '').replace('+', ''))
            col = _extract_colour(style)
            if week_pct is None:
                week_pct = val
                week_colour = col
            elif month_pct is None:
                month_pct = val
                month_colour = col
        elif txt in ("Elevated", "Low", "Moderate", "High", "Normal"):
            vol_str = txt
        elif re.match(r'^-?\d+\.\d+$', txt) and regime_score is None:
            regime_score = float(txt)
            regime_colour = _extract_colour(style)

    return {
        "exchange": exchange,
        "tip_date": tip_date,
        "market_state": market_state,
        "week_pct": week_pct,
        "week_colour": week_colour,
        "month_pct": month_pct,
        "month_colour": month_colour,
        "volatility": vol_str,
        "regime_score": regime_score,
        "regime_colour": regime_colour,
    }

# ---------------------------------------------------------------------------
# Individual tip card parsing
# ---------------------------------------------------------------------------

def _parse_tip_card(card_td, tip_n: int) -> dict:
    """Parse one tip card <td> into a dict, handling two different HTML formats."""
    # --- Common parsing for both formats ---
    # Ticker + URL: the <a> pointing to stockdataanalytics.com/news/
    ticker_a = card_td.find("a", href=re.compile(r'stockdataanalytics\.com/news/'))
    ticker = _clean(ticker_a.get_text()) if ticker_a else None
    url = ticker_a["href"] if ticker_a else None
    code = f"{ticker}.US" if ticker else None

    # Initialize all fields as None
    result = {
        "tip_n": tip_n,
        "code": code,
        "win_probability": None,
        "sector": None,
        "name": None,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "target": None,
        "stop": None,
        "expected_reward": None,
        "expected_risk": None,
        "holding_period_low": None,
        "holding_period_high": None,
        "url": url,
        "pattern_quality_score": None,
        "pattern_quality_colour": None,
        "setup_score": None,
        "setup_colour": None,
        "risk_reward_score": None,
        "risk_reward_colour": None,
        "context_score": None,
        "context_colour": None,
    }
    card_desc = f"{code or 'unknown'} tip_n={tip_n}"

    if tip_n <= 3:
        # --- Original format (first 3 tips) ---
        # Company name: <p> with font-size: 13px directly after ticker
        ticker_p = ticker_a.find_parent("p") if ticker_a else None
        if ticker_p:
            sib = ticker_p.find_next_sibling("p")
            if sib:
                result["name"] = _clean(sib.get_text())

        # Sector: inline-block span after name
        if ticker_p:
            span = ticker_p.find_next("span")
            if span:
                result["sector"] = _clean(span.get_text())

        # Win probability: large font-size: 32px paragraph
        for p in card_td.find_all("p", style=True):
            if "32px" in p.get("style", ""):
                txt = _clean(p.get_text()).replace("%", "")
                if txt.isdigit():
                    result["win_probability"] = int(txt)
                    break

        # Trade levels: Entry Zone / Target / Stop Loss
        for p in card_td.find_all("p", style=True):
            if "18px" in p.get("style", ""):
                sib = p.find_next_sibling("p")
                label = _clean(sib.get_text()) if sib else ""
                txt = _clean(p.get_text())
                if label == "Entry Zone":
                    m = re.match(r'\$?([\d.]+)-\$?([\d.]+)', txt)
                    if m:
                        result["entry_zone_low"] = float(m.group(1))
                        result["entry_zone_high"] = float(m.group(2))
                elif label == "Target":
                    entry = re.sub(r'[^\d.]', '', txt)
                    result["target"] = float(entry) if entry else None
                elif label == "Stop Loss":
                    entry = re.sub(r'[^\d.]', '', txt)
                    result["stop"] = float(entry) if entry else None

        # Exp. Reward / Exp. Risk / Hold Period: font-size: 20px paragraphs
        for p in card_td.find_all("p", style=True):
            if "20px" in p.get("style", ""):
                sib = p.find_next_sibling("p")
                label = _clean(sib.get_text()) if sib else ""
                txt = _clean(p.get_text())
                if "EXP. REWARD" in label.upper():
                    entry = re.sub(r'[^\d.]', '', txt)
                    result["expected_reward"] = float(entry) if entry else None
                elif "EXP. RISK" in label.upper():
                    entry = re.sub(r'[^\d.]', '', txt)
                    result["expected_risk"] = abs(float(entry)) if entry else None  # Store as positive
                elif "HOLD PERIOD" in label.upper():
                    m = re.match(r'(\d+)-(\d+)', txt)
                    if m:
                        result["holding_period_low"] = int(m.group(1))
                        result["holding_period_high"] = int(m.group(2))

        # Score bars: four <p> tags with font-size: 13px font-weight: 700,
        # in order Pattern Quality, Setup, Risk/Reward, Context.
        score_ps = [
            p for p in card_td.find_all("p", style=True)
            if "13px" in p.get("style", "") and "700" in p.get("style", "")
        ]
        if len(score_ps) != 4:
            logging.warning(f"{card_desc}: expected 4 score bars, found {len(score_ps)}.")
        if len(score_ps) >= 4:
            def _score_val(p):
                txt = _clean(p.get_text())
                try:
                    return float(txt)
                except Exception:
                    return None

            categories = ["pattern_quality", "setup", "risk_reward", "context"]
            for cat, p in zip(categories, score_ps):
                score = _score_val(p)
                colour = _extract_colour(p["style"])
                result[f"{cat}_score"] = score
                result[f"{cat}_colour"] = colour
                _check_score_bounds(score, cat, card_desc)
                _check_colour_consistency(score, colour, cat, card_desc)

            _check_total_score(
                card_td,
                {cat: result[f"{cat}_score"] for cat in categories},
                card_desc,
            )

    else:
        # --- New format (tips 4 and above) ---
        # Win probability: in a coloured circle <span> with "Win" below it
        win_span = card_td.find("span", string=re.compile(r'\d+%'))
        if not win_span:
            # Alternative: look for span with style containing background colour
            for span in card_td.find_all("span", style=True):
                if "background" in span.get("style", "").lower():
                    # Check if it contains a percentage
                    txt = _clean(span.get_text())
                    if "%" in txt:
                        win_span = span
                        break
        if win_span:
            txt = _clean(win_span.get_text()).replace("%", "")
            if txt.isdigit():
                result["win_probability"] = int(txt)

        # Sector: inline as small text next to ticker
        if ticker_a:
            # Look for text nodes near the ticker link
            parent = ticker_a.parent
            if parent:
                for sibling in parent.next_siblings:
                    if hasattr(sibling, 'name'):
                        if sibling.name == "span":
                            text = _clean(sibling.get_text())
                            if text and len(text) < 50:
                                result["sector"] = text
                                break
                    elif hasattr(sibling, 'strip'):
                        text = sibling.strip()
                        if text and len(text) < 50:
                            result["sector"] = text
                            break

        # Reward/risk: shown as +$X.XX reward / -$X.XX risk in a single <p>
        for p in card_td.find_all("p"):
            txt = _clean(p.get_text())
            if "reward" in txt.lower() and "risk" in txt.lower():
                reward_match = re.search(r'\+\$([\d.]+)', txt)
                risk_match = re.search(r'-\$([\d.]+)', txt)
                if reward_match:
                    result["expected_reward"] = float(reward_match.group(1))
                if risk_match:
                    result["expected_risk"] = abs(float(risk_match.group(1)))  # Store as positive
                break

        # Holding period: just 1-9d in blue inline
        for string in card_td.stripped_strings:
            if re.match(r'\d+-\d+d', string):
                m = re.match(r'(\d+)-(\d+)d', string)
                if m:
                    result["holding_period_low"] = int(m.group(1))
                    result["holding_period_high"] = int(m.group(2))
                break

        # Entry zone, target, stop: in <p> tags with specific formats
        for p in card_td.find_all("p"):
            txt = _clean(p.get_text())
            # Entry Zone: $X-$Y
            entry_match = re.match(r'\$([\d.]+)-\$([\d.]+)', txt)
            if entry_match and result["entry_zone_low"] is None:
                result["entry_zone_low"] = float(entry_match.group(1))
                result["entry_zone_high"] = float(entry_match.group(2))
            # Target: Target: $Z
            target_match = re.match(r'Target:\s*\$([\d.]+)', txt)
            if target_match:
                result["target"] = float(target_match.group(1))
            # Stop: Stop: $Z
            stop_match = re.match(r'Stop:\s*\$([\d.]+)', txt)
            if stop_match:
                result["stop"] = float(stop_match.group(1))

        # Mini score bars: four <td width="20"> cells in order Pattern
        # Quality, Setup, Risk/Reward, Context. Each contains a small
        # <table> with two stacked <td height=...> rows -- the empty
        # portion, then the coloured filled portion whose height and
        # background colour we read directly (structured traversal,
        # not string-splitting the raw HTML).
        categories = ["pattern_quality", "setup", "risk_reward", "context"]
        mini_tds = card_td.find_all("td", attrs={"width": "20"})
        if len(mini_tds) != 4:
            logging.warning(f"{card_desc}: expected 4 mini score bars, found {len(mini_tds)}.")

        for cat, td in zip(categories, mini_tds):
            tbl = td.find("table")
            if not tbl:
                continue
            height_tds = tbl.find_all("td", height=True)
            if len(height_tds) < 2:
                continue
            filled_td = height_tds[1]
            h_filled = int(filled_td["height"])
            colour = _extract_bg_colour(filled_td.get("style", ""))
            score = _height_to_score(h_filled, cat, BOX_HEIGHT_COMPACT)
            result[f"{cat}_score"] = score
            result[f"{cat}_colour"] = colour
            _check_score_bounds(score, cat, card_desc)
            _check_colour_consistency(score, colour, cat, card_desc)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_tip_email(
    eml_path: pathlib.Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse a single StockDataAnalytics tip email (.eml file).
    Returns
    -------
    (exchange_df, tips_df)
        exchange_df : 1-row DataFrame with exchange-level summary columns
        tips_df : one row per tip (up to 20)

    Colour fields are integers: 1=green, 2=yellow, 3=orange, 4=red.
    All .US suffix is appended to ticker codes automatically.
    tip_n is 1-based position within the email.
    """
    html = _get_html(eml_path)
    soup = BeautifulSoup(html, "lxml")

    # Exchange summary
    exc = _parse_exchange(soup, eml_path)
    exchange_df = pd.DataFrame([exc])
    exchange_df["tip_date"] = pd.to_datetime(exchange_df["tip_date"]).dt.date

    # Tip cards: each is the outermost <td> that contains exactly one
    # unique stockdataanalytics detail link, found via the border-bottom style.
    def _find_card_td(a_tag):
        p = a_tag.parent
        best = None
        while p and p.name != "[document]":
            if p.name == "td" and "border-bottom" in p.get("style", ""):
                best = p
            p = p.parent
        return best

    all_links = soup.find_all(
        "a", href=re.compile(r'stockdataanalytics\.com/news/')
    )
    seen = set()
    tip_rows = []
    for a in all_links:
        href_key = a["href"].split("?")[0]
        if href_key in seen:
            continue
        seen.add(href_key)
        card_td = _find_card_td(a)
        if card_td is None:
            logging.warning(f"Failed to find card for link: {a['href']}")
            continue
        tip_n = len(tip_rows) + 1
        parsed_tip = _parse_tip_card(card_td, tip_n)
        tip_rows.append(parsed_tip)

    tips_df = pd.DataFrame(tip_rows)

    # Add tip_date and exchange so tips can be joined to exchange_df
    tips_df.insert(0, "tip_date", exc["tip_date"])
    tips_df.insert(0, "exchange", exc["exchange"])

    # Type coercions
    for col in [
        "win_probability",
        "holding_period_low",
        "holding_period_high",
        "pattern_quality_colour",
        "setup_colour",
        "risk_reward_colour",
        "context_colour",
    ]:
        if col in tips_df.columns:
            tips_df[col] = pd.to_numeric(tips_df[col], errors="coerce").astype("Int64")

    for col in [
        "entry_zone_low",
        "entry_zone_high",
        "target",
        "stop",
        "expected_reward",
        "expected_risk",
        "pattern_quality_score",
        "setup_score",
        "risk_reward_score",
        "context_score",
    ]:
        if col in tips_df.columns:
            tips_df[col] = pd.to_numeric(tips_df[col], errors="coerce")

    return exchange_df, tips_df

def parse_tip_emails(
    eml_paths: list[pathlib.Path],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse multiple .eml files and concatenate results.
    Returns (exchange_df, tips_df) with rows from all emails.
    """
    exc_frames, tip_frames = [], []
    for path in eml_paths:
        e, t = parse_tip_email(path)
        exc_frames.append(e)
        tip_frames.append(t)
    exchange_df = pd.concat(exc_frames, ignore_index=True)
    tips_df = pd.concat(tip_frames, ignore_index=True)
    return exchange_df, tips_df

# ---------------------------------------------------------------------------
# Tip email SQLite persistence
# ---------------------------------------------------------------------------
_DDL_TIP_EXCHANGE = """
CREATE TABLE IF NOT EXISTS {tablename} (
    exchange TEXT NOT NULL,
    tip_date TEXT NOT NULL,
    market_state TEXT,
    week_pct REAL,
    week_colour INTEGER,
    month_pct REAL,
    month_colour INTEGER,
    volatility TEXT,
    regime_score REAL,
    regime_colour INTEGER,
    PRIMARY KEY (exchange, tip_date)
);
"""

_DDL_TIP_DETAILS = """
CREATE TABLE IF NOT EXISTS {tablename} (
    exchange TEXT NOT NULL,
    tip_date TEXT NOT NULL,
    tip_n INTEGER NOT NULL,
    code TEXT,
    win_probability INTEGER,
    sector TEXT,
    name TEXT,
    entry_zone_low REAL,
    entry_zone_high REAL,
    target REAL,
    stop REAL,
    expected_reward REAL,
    expected_risk REAL,
    holding_period_low INTEGER,
    holding_period_high INTEGER,
    url TEXT,
    pattern_quality_score REAL,
    pattern_quality_colour INTEGER,
    setup_score REAL,
    setup_colour INTEGER,
    risk_reward_score REAL,
    risk_reward_colour INTEGER,
    context_score REAL,
    context_colour INTEGER,
    PRIMARY KEY (exchange, tip_date, tip_n)
);
"""

_INSERT_TIP_EXCHANGE = """
INSERT OR REPLACE INTO {tablename} (
    exchange, tip_date, market_state, week_pct, week_colour,
    month_pct, month_colour, volatility, regime_score, regime_colour
)
VALUES (:exchange, :tip_date, :market_state, :week_pct, :week_colour,
        :month_pct, :month_colour, :volatility, :regime_score, :regime_colour);
"""

_INSERT_TIP_DETAILS = """
INSERT OR REPLACE INTO {tablename} (
    exchange, tip_date, tip_n, code, win_probability, sector, name,
    entry_zone_low, entry_zone_high, target, stop, expected_reward, expected_risk,
    holding_period_low, holding_period_high, url,
    pattern_quality_score, pattern_quality_colour,
    setup_score, setup_colour,
    risk_reward_score, risk_reward_colour,
    context_score, context_colour
)
VALUES (
    :exchange, :tip_date, :tip_n, :code, :win_probability, :sector, :name,
    :entry_zone_low, :entry_zone_high, :target, :stop, :expected_reward, :expected_risk,
    :holding_period_low, :holding_period_high, :url,
    :pattern_quality_score, :pattern_quality_colour,
    :setup_score, :setup_colour,
    :risk_reward_score, :risk_reward_colour,
    :context_score, :context_colour
);
"""

def tips_exchange2sqlite(
    exchange_df: pd.DataFrame,
    tips_df: pd.DataFrame,
    db: Union[str, pathlib.Path, Database],
    exchange_tablename: str = "tip_exchange",
    tips_tablename: str = "tip_details",
) -> None:
    """Write exchange_df and tips_df to SQLite.
    INSERT OR REPLACE — idempotent; safe to call after each email import.
    Primary key on exchange table: (exchange, tip_date).
    Primary key on tips table: (exchange, tip_date, tip_n).
    """
    if isinstance(db, Database):
        pass
    elif isinstance(db, (str, pathlib.Path)):
        db = Database(db)

    db.conn.execute(_DDL_TIP_EXCHANGE.format(tablename=exchange_tablename))
    db.conn.execute(_DDL_TIP_DETAILS.format(tablename=tips_tablename))

    for row in exchange_df.itertuples(index=False):
        d = dict(row._asdict())
        d["tip_date"] = (
            row.tip_date.isoformat()
            if isinstance(row.tip_date, date)
            else str(row.tip_date)
        )
        db.conn.execute(
            _INSERT_TIP_EXCHANGE.format(tablename=exchange_tablename), d
        )

    for row in tips_df.itertuples(index=False):
        d = dict(row._asdict())
        d["tip_date"] = (
            row.tip_date.isoformat()
            if isinstance(row.tip_date, date)
            else str(row.tip_date)
        )
        # Convert pandas NA / numpy int types to plain Python for sqlite3
        for k, v in d.items():
            try:
                if pd.isna(v):
                    d[k] = None
                    continue
            except (TypeError, ValueError):
                pass
            if hasattr(v, "item"):  # numpy/pandas scalar → Python native
                d[k] = v.item()
        db.conn.execute(
            _INSERT_TIP_DETAILS.format(tablename=tips_tablename), d
        )
    db.conn.commit()


def tips_sqlite2pandas(
    db: Union[sqlite3.Connection, str, pathlib.Path],
    exchange_tablename: str = "tip_exchange",
    tips_tablename: str = "tip_details",
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read tip tables back from SQLite into pandas DataFrames.
    Parameters
    ----------
    start / end : optional date range filter on tip_date (inclusive).
    """
    _own = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db) if _own else db
    try:
        conditions, params = [], []
        if start:
            conditions.append("tip_date >= ?")
            params.append(start.isoformat())
        if end:
            conditions.append("tip_date <= ?")
            params.append(end.isoformat())
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        exchange_df = pd.read_sql(
            f"SELECT * FROM {exchange_tablename} {where}", conn, params=params
        )
        tips_df = pd.read_sql(
            f"SELECT * FROM {tips_tablename} {where}", conn, params=params
        )
    finally:
        if _own:
            conn.close()

    # Restore date type
    exchange_df["tip_date"] = pd.to_datetime(exchange_df["tip_date"]).dt.date
    tips_df["tip_date"] = pd.to_datetime(tips_df["tip_date"]).dt.date

    # Restore nullable ints for colour columns
    int_cols = ["week_colour", "month_colour", "regime_colour"]
    for col in int_cols:
        if col in exchange_df.columns:
            exchange_df[col] = exchange_df[col].astype("Int64")

    tip_int_cols = [
        "win_probability",
        "holding_period_low",
        "holding_period_high",
        "pattern_quality_colour",
        "setup_colour",
        "risk_reward_colour",
        "context_colour",
    ]
    for col in tip_int_cols:
        if col in tips_df.columns:
            tips_df[col] = tips_df[col].astype("Int64")

    return exchange_df, tips_df