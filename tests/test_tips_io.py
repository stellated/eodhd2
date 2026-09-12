import logging
import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from src.tips_io import (
    CATEGORY_MAX,
    _get_html,
    _hex_to_int,
    _parse_tip_card,
    parse_tip_email,
    tips_exchange2sqlite,
    tips_sqlite2pandas,
)

### **Email Parsing Tests**

def test_parse_tip_email_april_2026():
    # Every captured email (April 2026 through the current archive) uses
    # the same 3 full-detail + 17 compact card layout -- there is no era
    # where all 20 cards use the full format (see doc/DESIGN_DECISIONS.md,
    # "Unified per-quality score column").
    exchange_df, tips_df = parse_tip_email(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"))
    assert len(exchange_df) == 1
    assert len(tips_df) == 20
    assert "exchange" in exchange_df.columns
    assert "tip_date" in tips_df.columns

def test_parse_tip_email_june_2026():
    # Same layout as April: 3 full cards (tip_n 1-3) + 17 compact cards
    # (tip_n 4-20).
    exchange_df, tips_df = parse_tip_email(Path("tests/data/eml/2026-06-10_Daily_Stock_Pick.eml"))
    assert len(exchange_df) == 1
    assert len(tips_df) == 20
    # Compact cards (tip_n > 3) have no printed number in the HTML, but as
    # of the 2026-09-12 score/height unification they DO get an estimated
    # score for all four qualities, reconstructed from the bar's fill
    # height -- not None, and within that quality's known range.
    compact = tips_df.loc[tips_df["tip_n"] > 3]
    for quality in ["pattern_quality", "setup", "risk_reward", "context"]:
        scores = compact[f"{quality}_score"]
        assert scores.notna().all()
        assert (scores >= 0).all()
        assert (scores <= CATEGORY_MAX[quality]).all()

### **Colour Extraction Tests**

def test_hex_to_int():
    assert _hex_to_int("#22c55e") == 1  # green
    assert _hex_to_int("#eab308") == 2  # yellow
    assert _hex_to_int("#f97316") == 3  # orange
    assert _hex_to_int("#ef4444") == 4  # red
    assert _hex_to_int("#unknown") is None

def test_hex_to_int_unrecognized(caplog):
    # Test warning for unrecognized colour
    with caplog.at_level(logging.WARNING):
        _hex_to_int("#unknown")
        assert "Unrecognized colour: #unknown" in caplog.text

### **SQLite Persistence Tests**

def test_tips_roundtrip(tmp_path):
    # Test round-trip: parse -> SQLite -> pandas
    exchange_df, tips_df = parse_tip_email(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"))
    db_path = tmp_path / "tips.db"
    tips_exchange2sqlite(exchange_df, tips_df, db_path)
    exchange_df2, tips_df2 = tips_sqlite2pandas(db_path)
    # exchange_df's colour columns are plain int64 fresh from parsing (no
    # nulls ever coerced for it, unlike tips_df) but come back as nullable
    # Int64 from SQLite (tips_sqlite2pandas defensively allows for NULLs).
    # Values match exactly; only the pandas dtype representation differs,
    # so compare values only for this one.
    pd.testing.assert_frame_equal(exchange_df, exchange_df2, check_dtype=False)
    assert tips_df.equals(tips_df2)

### **Card Parsing Tests**

def _find_card_tds(html: bytes) -> list:
    """Locate every tip card <td>, the same way parse_tip_email() does:
    each card is the outermost <td> with border-bottom in its style that
    encloses a unique stockdataanalytics.com/news/ link. A naive
    `find_all("td", style=lambda x: "border-bottom" in x)` also matches
    unrelated tds elsewhere in the email (section dividers etc.), so this
    walks up from each ticker link instead, matching _parse_tip_email's
    own (private, not importable) card-finding logic.
    """
    soup = BeautifulSoup(html, "lxml")

    def find_card_td(a_tag):
        p = a_tag.parent
        best = None
        while p and p.name != "[document]":
            if p.name == "td" and "border-bottom" in (p.get("style") or ""):
                best = p
            p = p.parent
        return best

    seen = set()
    cards = []
    for a in soup.find_all("a", href=re.compile(r"stockdataanalytics\.com/news/")):
        href_key = a["href"].split("?")[0]
        if href_key in seen:
            continue
        seen.add(href_key)
        card_td = find_card_td(a)
        if card_td is not None:
            cards.append(card_td)
    return cards

def test_parse_tip_card_full():
    # Test full card parsing
    html = _get_html(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"))
    card_td = _find_card_tds(html)[0]
    result = _parse_tip_card(card_td, 1)
    assert result["code"] is not None
    assert result["win_probability"] is not None
    assert result["pattern_quality_score"] is not None

def test_parse_tip_card_compact():
    # Test compact card parsing
    html = _get_html(Path("tests/data/eml/2026-06-10_Daily_Stock_Pick.eml"))
    cards = _find_card_tds(html)
    result = _parse_tip_card(cards[4], 5)  # 5th card (compact)
    assert result["code"] is not None
    # Compact cards have an estimated (not printed) score -- see
    # test_parse_tip_email_june_2026 above.
    assert result["pattern_quality_score"] is not None
    assert 0 <= result["pattern_quality_score"] <= CATEGORY_MAX["pattern_quality"]
