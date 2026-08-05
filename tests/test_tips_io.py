
### **Email Parsing Tests**

def test_parse_tip_email_april_2026():
    # Test April 2026 format (all full cards)
    exchange_df, tips_df = parse_tip_email(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"))
    assert len(exchange_df) == 1
    assert len(tips_df) == 20  # April had 20 full cards
    assert "exchange" in exchange_df.columns
    assert "tip_date" in tips_df.columns

def test_parse_tip_email_june_2026():
    # Test June 2026 format (3 full + 17 compact cards)
    exchange_df, tips_df = parse_tip_email(Path("tests/data/eml/2026-06-10_Daily_Stock_Pick.eml"))
    assert len(exchange_df) == 1
    assert len(tips_df) == 20
    # Check that compact cards have None for numeric scores
    assert tips_df.loc[tips_df["tip_n"] > 3, "pattern_quality_number"].isna().all()

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
    assert exchange_df.equals(exchange_df2)
    assert tips_df.equals(tips_df2)

### **Card Parsing Tests**

def test_parse_tip_card_full():
    # Test full card parsing
    with open(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"), "rb") as f:
        html = _get_html(Path("tests/data/eml/2026-04-08_Daily_Stock_Pick.eml"))
    soup = BeautifulSoup(html, "lxml")
    card_td = soup.find("td", style=lambda x: x and "border-bottom" in x)
    result = _parse_tip_card(card_td, 1)
    assert result["code"] is not None
    assert result["win_probability"] is not None

def test_parse_tip_card_compact():
    # Test compact card parsing
    with open(Path("tests/data/eml/2026-06-10_Daily_Stock_Pick.eml"), "rb") as f:
        html = _get_html(Path("tests/data/eml/2026-06-10_Daily_Stock_Pick.eml"))
    soup = BeautifulSoup(html, "lxml")
    cards = soup.find_all("td", style=lambda x: x and "border-bottom" in x)
    result = _parse_tip_card(cards[4], 5)  # 5th card (compact)
    assert result["code"] is not None
    assert result["pattern_quality_number"] is None  # Compact cards have no numeric scores

