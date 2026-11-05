from datetime import date, timedelta

import pandas as pd

from scripts.jml_screen import (
    band,
    duplicate_groups,
    five_year_dates,
    five_years_before,
    median_turnover,
    screen_dates,
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
