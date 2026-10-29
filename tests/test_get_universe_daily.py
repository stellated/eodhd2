import json
import sqlite3
from datetime import date, timedelta

from scripts.get_universe_daily import (
    BENCHMARK_START,
    BENCHMARKS,
    LOOKBACK_DAYS,
    eodhd_code,
    fetch_plan,
    first_appearances,
)


def _alphanume_db(path, windows):
    """windows: (job, superseded, completed, rows)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE windows (id INTEGER PRIMARY KEY, job TEXT, capture TEXT, "
                 "completed_at TEXT, superseded INTEGER)")
    conn.execute("CREATE TABLE raw_responses "
                 "(id INTEGER PRIMARY KEY, window_id INTEGER, body TEXT)")
    for i, (job, superseded, completed, rows) in enumerate(windows, 1):
        conn.execute("INSERT INTO windows VALUES (?, ?, 'history', ?, ?)",
                     (i, job, "2026-10-27" if completed else None, superseded))
        conn.execute("INSERT INTO raw_responses (window_id, body) VALUES (?, ?)",
                     (i, json.dumps({"data": rows})))
    conn.commit()
    return conn


def test_eodhd_code_maps_class_suffix():
    assert eodhd_code("BRK.B") == "BRK-B.US"
    assert eodhd_code("aapl") == "AAPL.US"


def test_first_appearances_takes_earliest_and_skips_superseded_and_incomplete(tmp_path):
    conn = _alphanume_db(tmp_path / "a.db", [
        ("optionable", 0, True, [{"date": "2019-02-01", "ticker": "A"},
                                 {"date": "2019-01-02", "ticker": "A"}]),
        ("qgmi", 0, True, [{"date": "2020-03-02", "ticker": "Q", "rank": 1}]),
        ("optionable", 1, True, [{"date": "2018-01-02", "ticker": "A"}]),
        ("optionable", 0, False, [{"date": "2018-06-01", "ticker": "B"}]),
        ("ndm", 0, True, [{"date": "2018-01-02", "ticker": "N"}]),
    ])
    assert first_appearances(conn) == {"A": date(2019, 1, 2), "Q": date(2020, 3, 2)}


def test_fetch_plan_adds_lookback_and_benchmarks():
    plan = fetch_plan({"A": date(2019, 1, 2), "XLK": date(2017, 1, 3)})
    by_code = {code: (t, start) for t, code, start in plan}
    assert by_code["A.US"] == ("A", date(2019, 1, 2) - timedelta(days=LOOKBACK_DAYS))
    # a benchmark that is also a universe ticker keeps the earlier start
    assert by_code["XLK.US"][1] == date(2017, 1, 3) - timedelta(days=LOOKBACK_DAYS)
    assert by_code["SPY.US"] == ("SPY", BENCHMARK_START)
    assert len(plan) == 1 + len(BENCHMARKS)
