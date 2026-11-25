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
    db.execute("CREATE TABLE daily (code TEXT, date TEXT, vo INTEGER, ac REAL DEFAULT 1.0)")
    days = [(date(2014, 11, 3) + timedelta(days=i)).isoformat() for i in range(0, 1830)]
    rows = [(d,) for d in days]
    db.executemany("INSERT INTO daily (code, date, vo) VALUES ('SPY.US', ?, 1)", rows)
    db.executemany("INSERT INTO daily (code, date, vo) VALUES ('OK.US', ?, 1)", rows)
    db.executemany("INSERT INTO daily (code, date, vo) VALUES ('THIN.US', ?, ?)",
                   [(d, int(i % 5 != 0)) for i, d in enumerate(days)])  # 80% real
    db.executemany("INSERT INTO daily (code, date, vo) VALUES ('VIENNA.US', ?, ?)",
                   [(d, int(d[5:] != "12-26")) for d in days])
    start = date(2019, 11, 1)
    assert pre_period_problem(db, "OK.US", start) is None
    assert "80%" in pre_period_problem(db, "THIN.US", start)
    assert "26 December" in pre_period_problem(db, "VIENNA.US", start)
    assert "Russian" in pre_period_problem(db, "NLMK.US", start)


def _jump_db(tmp_path, code, moves):
    """SPY and `code` trading every day; `code`'s ac multiplied by each
    {date: factor} from that date on."""
    import sqlite3
    db = sqlite3.connect(tmp_path / "j.db")
    db.execute("CREATE TABLE daily (code TEXT, date TEXT, vo INTEGER, ac REAL)")
    days = [(date(2014, 11, 3) + timedelta(days=i)).isoformat() for i in range(0, 1830)]
    db.executemany("INSERT INTO daily VALUES ('SPY.US', ?, 1, 1.0)", [(d,) for d in days])
    ac = 10.0
    for d in days:
        ac *= moves.get(d, 1.0)
        db.execute("INSERT INTO daily VALUES (?, ?, 1, ?)", (code, d, ac))
    return db


def test_ac_jumps_finds_moves_over_3x_either_way(tmp_path):
    from scripts.jml_screen import ac_jumps
    db = _jump_db(tmp_path, "J.US", {"2016-01-04": 0.25, "2017-03-01": 2.9, "2018-05-01": 3.5})
    assert [(d, round(r, 2)) for d, r in ac_jumps(db, "J.US", "2015-01-01", "2019-11-01")] == \
        [("2016-01-04", 0.25), ("2018-05-01", 3.5)]
    assert ac_jumps(db, "J.US", "2016-01-05", "2018-05-01") == []  # [lo, hi)


def test_pre_period_jumps_need_a_verdict(tmp_path, monkeypatch):
    import scripts.jml_screen as js
    csv = tmp_path / "v.csv"
    csv.write_text("code,date,ratio,verdict,kind,note,source\n"
                   "REAL.US,2016-01-04,0.25,real,trial_result,trial failed,x\n"
                   "BAD.US,2016-01-04,0.25,bad,splice,two companies,\n")
    monkeypatch.setattr(js, "JUMP_VERDICT_FILES", [csv, tmp_path / "missing.csv"])
    monkeypatch.setattr(js, "_verdicts", None)
    start = date(2019, 11, 1)
    db = _jump_db(tmp_path, "REAL.US", {"2016-01-04": 0.25})
    assert js.pre_period_problem(db, "REAL.US", start) is None
    db.close()
    (tmp_path / "j.db").unlink()
    db = _jump_db(tmp_path, "BAD.US", {"2016-01-04": 0.25})
    assert "splice" in js.pre_period_problem(db, "BAD.US", start)
    db.close()
    (tmp_path / "j.db").unlink()
    db = _jump_db(tmp_path, "NEW.US", {"2016-01-04": 0.25})
    with pytest.raises(SystemExit, match="no verdict"):
        js.pre_period_problem(db, "NEW.US", start)
    db.close()
    (tmp_path / "j.db").unlink()
    # a move after the period start is never a pre-period problem
    db = _jump_db(tmp_path, "LATE.US", {"2019-11-05": 0.25})
    assert js.pre_period_problem(db, "LATE.US", start) is None


def test_jump_verdicts_reject_an_unknown_kind(tmp_path, monkeypatch):
    import scripts.jml_screen as js
    csv = tmp_path / "v.csv"
    csv.write_text("code,date,ratio,verdict,kind,note,source\nX.US,2016-01-04,4,real,hunch,,\n")
    monkeypatch.setattr(js, "JUMP_VERDICT_FILES", [csv])
    monkeypatch.setattr(js, "_verdicts", None)
    with pytest.raises(SystemExit, match="bad verdict/kind"):
        js.jump_verdicts()



# --- load_events (2026-11-23) ---------------------------------------------

import sqlite3  # noqa: E402

import pytest  # noqa: E402

import scripts.jml_screen as js  # noqa: E402

EVENT_HEADER = ",".join(js.EVENT_COLUMNS)


class _FakeApi:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))

        class R:
            text = ("Date,Open,High,Low,Close,Adjusted_close,Volume\n"
                    "2020-06-30,2,2,2,2,2,100\n")
        return R()


def _events_db(tmp_path):
    db = sqlite3.connect(tmp_path / "jml.db")
    db.executescript("""
        CREATE TABLE stock_pool (code TEXT PRIMARY KEY, sample TEXT);
        INSERT INTO stock_pool VALUES ('IN.US', 'in_sample'), ('HOLD.US', 'hold_out');
        CREATE TABLE draw (period INTEGER, code TEXT, sample TEXT);
        INSERT INTO draw VALUES (1, 'IN.US', 'in_sample'), (1, 'HOLD.US', 'hold_out');
        CREATE TABLE fetch_status (period INTEGER, code TEXT, last_date TEXT);
        INSERT INTO fetch_status VALUES (1, 'IN.US', '2020-06-30'), (1, 'HOLD.US', '2020-06-30');
    """)
    return db


def _write_events(tmp_path, monkeypatch, row):
    csv = tmp_path / "events.csv"
    csv.write_text(EVENT_HEADER + "\n" + row + "\n")
    monkeypatch.setattr(js, "EVENTS_CSV", csv)


def test_load_events_stores_events_and_follow_on_bars(tmp_path, monkeypatch):
    db = _events_db(tmp_path)
    _write_events(tmp_path, monkeypatch,
                  "1,IN.US,stock_merger,,2020-07-01,2020-06-30,,ACQ.US,0.5,USD,,,,,src,")
    api = _FakeApi()
    js.load_events(db, api)
    assert db.execute("SELECT code, follow_code FROM series_events").fetchall() == [
        ("IN.US", "ACQ.US")]
    assert db.execute("SELECT code, date, cl FROM follow_on_daily").fetchall() == [
        ("ACQ.US", "2020-06-30", 2.0)]
    assert api.calls[0][1]["from"] == "2020-05-16"  # 45 days before the stop


def test_load_events_refuses_a_hold_out_stock_period(tmp_path, monkeypatch):
    db = _events_db(tmp_path)
    _write_events(tmp_path, monkeypatch, "1,HOLD.US,cash_takeover,,,,10,,,,,,,,src,")
    with pytest.raises(SystemExit, match="not an in-sample"):
        js.load_events(db, _FakeApi())


def test_load_events_refuses_a_hold_out_follow_on(tmp_path, monkeypatch):
    db = _events_db(tmp_path)
    _write_events(tmp_path, monkeypatch,
                  "1,IN.US,stock_merger,,,,,HOLD.US,1,USD,,,,,src,")
    with pytest.raises(SystemExit, match="hold-out stock"):
        js.load_events(db, _FakeApi())
