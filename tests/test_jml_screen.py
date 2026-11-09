from datetime import date, timedelta

import pandas as pd

from scripts.jml_screen import (
    band,
    draw_band,
    duplicate_groups,
    five_year_dates,
    five_years_before,
    hold_out_pool,
    median_turnover,
    not_shares_by_code,
    pre_period_problem,
    screen_dates,
    stock_period_span,
    venue,
)

SESSIONS = [date(2019, 1, 1) + timedelta(days=i) for i in range(400)]


def test_screen_dates_end_the_session_before_start():
    d = screen_dates(SESSIONS, date(2019, 11, 1))
    assert len(d) == 60
    assert d[-1] == date(2019, 10, 31)


def test_five_year_dates_end_on_or_before_the_cut():
    sessions = [date(2014, 10, 27) + timedelta(days=i) for i in range(10)]
    d = five_year_dates(sessions, date(2019, 11, 1))
    assert d[-1] == date(2014, 11, 1)
    assert len(d) == 5


def test_five_years_before_29_february():
    assert five_years_before(date(2024, 2, 29)) == date(2019, 2, 28)


def test_venue():
    assert venue("AAPL", "NASDAQ", "US0378331005", "Apple Inc.") == "listed"
    assert venue("SBNY", "PINK", "US82669G1040", "Signature Bank") == "inferred"  # failed since
    assert venue("RHHBY", "OTCQX", "US7711951043", "Roche Holding Ltd ADR") == "foreign_otc"
    assert venue("TOELY", "PINK", "US8891101027", "Tokyo Electron Ltd PK") == "foreign_otc"
    assert venue("NESN", "OTCQX", "CH0038863350", "Nestle") == "foreign_otc"
    assert venue("ABCD", "PINK", None, None) == "foreign_otc"


def test_band_lower_bounds():
    assert band(50e6) == "above_50m"
    assert band(49.9e6) == "10m_50m"
    assert band(1e6) == "1m_10m"
    assert band(0.9e6) is None


def test_median_turnover_counts_a_missing_day_as_zero():
    dates = [date(2019, 1, d) for d in (2, 3, 4)]
    rows = pd.DataFrame({"date": ["2019-01-02", "2019-01-03"], "code": ["A", "A"],
                         "cl": [10.0, 10.0], "vo": [100.0, 300.0]})
    assert median_turnover(rows, dates)["A"] == 1000.0  # median of 1000, 3000, 0


def test_duplicate_groups_match_identical_closes_only():
    dates = [date(2019, 1, 2), date(2019, 1, 3)]
    rows = pd.DataFrame({
        "date": ["2019-01-02", "2019-01-03"] * 3,
        "code": ["SQ", "SQ", "XYZ", "XYZ", "AAPL", "AAPL"],
        "cl": [62.6, 63.0, 62.6, 63.0, 62.6, 64.0],
        "vo": [1.0] * 6,
    })
    assert duplicate_groups(rows, dates) == [["SQ", "XYZ"]]


def test_duplicate_groups_allow_a_stray_close_but_not_a_share_class():
    dates = [date(2019, 1, 1) + timedelta(days=i) for i in range(60)]
    days = [d.isoformat() for d in dates]
    closes = [10.0 + i / 10 for i in range(60)]
    stray = closes[:-1] + [99.0]  # one bad day
    rows = pd.DataFrame({
        "date": days * 3,
        "code": ["BALL"] * 60 + ["BLL"] * 60 + ["GOOG"] * 60,
        "cl": closes + stray + [x * 1.003 for x in closes],  # a share class: 0.3% apart
        "vo": [1000.0] * 60 + [600.0] * 60 + [1000.0] * 60,
    })
    assert duplicate_groups(rows, dates) == [["BALL", "BLL"]]


def test_draw_band_is_reproducible():
    codes = [f"C{i:03d}.US" for i in range(500)]
    picked = draw_band(codes, 1, 0, 0, 40)
    assert picked == draw_band(list(reversed(codes)), 1, 0, 0, 40)  # order of input ignored
    assert len(set(picked)) == 40
    assert draw_band(codes, 2, 0, 0, 40) != picked


def test_hold_out_pool_is_a_quarter_of_stocks():
    codes = [f"C{i:03d}.US" for i in range(400)]
    pool = hold_out_pool(codes + codes[:50])  # a stock eligible in two periods counts once
    assert len(pool) == 100
    assert pool == hold_out_pool(codes)


def test_stock_period_span():
    lo, hi = stock_period_span(date(2019, 11, 1), date(2019, 12, 31))
    assert lo == date(2014, 10, 18)
    assert hi == date(2021, 1, 14)


def test_not_shares_by_code():
    for code in ("GIX-UN", "GIX-U", "LHC-WS", "FITB-P-I", "ISF-CL"):
        assert not_shares_by_code(code), code
    for code in ("BF-B", "HEI-A", "MOG-A", "BRK-B", "AAPL", "HTZ-R-W"):
        assert not not_shares_by_code(code), code


def test_pre_period_problem(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "j.db")
    db.execute("CREATE TABLE daily (code TEXT, date TEXT, vo INTEGER)")
    days = [(date(2014, 11, 3) + timedelta(days=i)).isoformat() for i in range(0, 1830)]
    db.executemany("INSERT INTO daily VALUES ('SPY.US', ?, 1)", [(d,) for d in days])
    db.executemany("INSERT INTO daily VALUES ('OK.US', ?, 1)", [(d,) for d in days])
    db.executemany("INSERT INTO daily VALUES ('THIN.US', ?, ?)",
                   [(d, int(i % 5 != 0)) for i, d in enumerate(days)])  # 80% real
    db.executemany("INSERT INTO daily VALUES ('VIENNA.US', ?, ?)",
                   [(d, int(d[5:] != "12-26")) for d in days])
    start = date(2019, 11, 1)
    assert pre_period_problem(db, "OK.US", start) is None
    assert "80%" in pre_period_problem(db, "THIN.US", start)
    assert "26 December" in pre_period_problem(db, "VIENNA.US", start)
    assert "Russian" in pre_period_problem(db, "NLMK.US", start)

