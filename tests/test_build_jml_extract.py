import sqlite3
from datetime import date

import pytest

import scripts.build_jml_extract as bje


@pytest.fixture
def jml_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bje, "PERIODS", [(1, date(2019, 11, 1), date(2019, 12, 31))])
    monkeypatch.setattr(bje, "DRAW_SIZES", {"above_50m": (1, 1)})
    path = tmp_path / "jml.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE stock_pool (code TEXT PRIMARY KEY, sample TEXT);
        INSERT INTO stock_pool VALUES ('IN.US', 'in_sample'), ('HOLD.US', 'hold_out'),
            ('POOL.US', 'hold_out');
        CREATE TABLE draw (period INTEGER, code TEXT, band TEXT, sample TEXT, draw_rank INTEGER,
            inclusion_prob REAL, eligible_in_pool INTEGER, seed INTEGER, drawn_at TEXT);
        INSERT INTO draw VALUES (1, 'IN.US', 'above_50m', 'in_sample', 1, 1.0, 1, 1, 'x'),
            (1, 'HOLD.US', 'above_50m', 'hold_out', 1, 0.5, 2, 1, 'x');
        CREATE TABLE eligible (period INTEGER, code TEXT, band TEXT);
        INSERT INTO eligible VALUES (1, 'IN.US', 'above_50m'), (1, 'HOLD.US', 'above_50m'),
            (1, 'POOL.US', 'above_50m');
        CREATE TABLE screen_dropped (period INTEGER, code TEXT, reason TEXT, detail TEXT);
        INSERT INTO screen_dropped VALUES (1, 'OLDPOOL.US', 'duplicate_of', 'POOL'),
            (1, 'JUNK.US', 'price_below_2', '1.5');
        CREATE TABLE fetch_status (period INTEGER, code TEXT, from_date TEXT, to_date TEXT,
            status TEXT, rows INTEGER, first_date TEXT, last_date TEXT, error TEXT,
            fetched_at TEXT);
        INSERT INTO fetch_status VALUES
            (1, 'IN.US', '2014-10-18',
             '2021-01-14', 'ok', 3, '2014-10-20', '2020-06-30', NULL, 'x'),
            (1, 'HOLD.US', '2014-10-18',
             '2021-01-14', 'ok', 1, '2014-10-20', '2021-01-13', NULL, 'x'),
            (0, 'SPY.US', '2014-10-18',
             '2021-01-14', 'ok', 1, '2014-10-20', '2021-01-13', NULL, 'x');
        CREATE TABLE daily (code TEXT, timestamp INTEGER, date TEXT, cl REAL, vo INTEGER,
            PRIMARY KEY (code, timestamp));
        INSERT INTO daily VALUES ('IN.US', 1, '2014-10-20', 1, 1), ('IN.US', 2, '2020-06-30', 1, 1),
            ('IN.US', 3, '2023-01-03', 1, 1),  -- outside its span
            ('HOLD.US', 1, '2015-01-02', 1, 1), ('SPY.US', 1, '2015-01-02', 1, 1);
    """)
    db.commit()
    db.close()
    return path


def test_extract_holds_nothing_on_hold_out_stocks(jml_db, tmp_path):
    out = tmp_path / "out.db"
    bje.build(jml_db, out)
    db = sqlite3.connect(out)
    for table in ("draw", "daily", "fetch_status", "eligible", "screen_dropped", "series_gaps"):
        codes = {c for (c,) in db.execute(f"SELECT code FROM {table}")}
        assert not codes & {"HOLD.US", "POOL.US", "OLDPOOL.US"}, table
    assert db.execute("SELECT COUNT(*) FROM screen_dropped").fetchone()[0] == 1  # JUNK only
    assert db.execute("SELECT hold_out_pool FROM pool_counts").fetchone()[0] == 2


def test_extract_keeps_bars_inside_spans_and_lists_short_series(jml_db, tmp_path):
    out = tmp_path / "out.db"
    bje.build(jml_db, out)
    db = sqlite3.connect(out)
    assert sorted(db.execute("SELECT code, date FROM daily")) == [
        ("IN.US", "2014-10-20"), ("IN.US", "2020-06-30"), ("SPY.US", "2015-01-02")]
    assert db.execute("SELECT code, kind FROM series_gaps").fetchall() == [
        ("IN.US", "stops_in_outcome_year")]
