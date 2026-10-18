import sys
from datetime import date, timedelta

import pytest

from scripts.daily_update import (
    _all_tips,
    _log_dead_intraday,
    _log_stale_tickers,
    _log_unresolved_tips,
    _still_open_tips,
    main,
)
from src.eodhd_io import Database


def _seed_tip_details(db: Database, rows: list[tuple[str, str, int, str]]) -> None:
    db.conn.execute(
        """
        CREATE TABLE tip_details (
            exchange TEXT NOT NULL,
            tip_date TEXT NOT NULL,
            tip_n INTEGER NOT NULL,
            code TEXT,
            PRIMARY KEY (exchange, tip_date, tip_n)
        )
        """
    )
    db.conn.executemany(
        "INSERT INTO tip_details (exchange, tip_date, tip_n, code) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.conn.commit()


def test_still_open_tips_filters_by_cutoff_and_dedupes(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(
            db,
            [
                ("NASDAQ", "2026-09-01", 1, "OLD.US"),
                ("NASDAQ", "2026-09-20", 1, "RECENT.US"),
                ("NASDAQ", "2026-09-20", 2, "RECENT.US"),  # same (code, tip_date), dup tip_n
                ("NASDAQ", "2026-09-25", 1, "NEWEST.US"),
            ],
        )

        result = _still_open_tips(db, "tip_details", cutoff=date(2026, 9, 15))

    assert set(result) == {
        ("RECENT.US", date(2026, 9, 20)),
        ("NEWEST.US", date(2026, 9, 25)),
    }


def test_still_open_tips_missing_table_returns_empty(tmp_path):
    with Database(tmp_path / "test.db") as db:
        result = _still_open_tips(db, "tip_details", cutoff=date(2026, 1, 1))

    assert result == []


def test_all_tips_returns_full_history_not_just_recent(tmp_path):
    """Unlike _still_open_tips(), _all_tips() must include tips whose
    window has long closed -- that's exactly what unresolved_tips() needs
    to check."""
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(
            db,
            [
                ("NASDAQ", "2020-01-01", 1, "ANCIENT.US"),
                ("NASDAQ", "2026-09-25", 1, "RECENT.US"),
            ],
        )

        result = _all_tips(db, "tip_details")

    assert set(result) == {
        ("ANCIENT.US", date(2020, 1, 1)),
        ("RECENT.US", date(2026, 9, 25)),
    }


def test_all_tips_missing_table_returns_empty(tmp_path):
    with Database(tmp_path / "test.db") as db:
        result = _all_tips(db, "tip_details")

    assert result == []


def _seed_daily_dates(db: Database, rows: list[tuple[str, str]]) -> None:
    db.conn.execute("CREATE TABLE daily (code TEXT, date TEXT)")
    db.conn.executemany("INSERT INTO daily (code, date) VALUES (?, ?)", rows)
    db.conn.commit()


def test_log_unresolved_tips_warns_on_closed_short_windows(tmp_path, caplog):
    today = date.today()
    closed_tip_date = today - timedelta(days=100)  # well past any backfill window

    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [("NASDAQ", closed_tip_date.isoformat(), 1, "BAD.US")])
        # Only one real row after the tip -- short of the default n2=20 days,
        # and the window closed long ago, so this must be flagged.
        _seed_daily_dates(db, [("BAD.US", closed_tip_date.isoformat())])

        with caplog.at_level("WARNING"):
            _log_unresolved_tips(db)

    assert any("BAD.US" in rec.message for rec in caplog.records)
    assert any("unresolved tip" in rec.message for rec in caplog.records)


def _seed_full_daily_and_intraday(db: Database, code: str, tip_date: date, intraday: bool) -> None:
    """21 days of daily coverage from tip_date (enough for default n2=20),
    and optionally the same days in intraday_5m."""
    days = [(code, (tip_date + timedelta(days=i)).isoformat()) for i in range(21)]
    db.conn.execute("CREATE TABLE IF NOT EXISTS daily (code TEXT, date TEXT)")
    db.conn.executemany("INSERT INTO daily (code, date) VALUES (?, ?)", days)
    db.conn.execute("CREATE TABLE IF NOT EXISTS intraday_5m (code TEXT, local_date TEXT)")
    if intraday:
        db.conn.executemany("INSERT INTO intraday_5m (code, local_date) VALUES (?, ?)", days)
    db.conn.commit()


def test_log_unresolved_tips_checks_intraday_too(tmp_path, caplog):
    """Full daily coverage but no 5m window fetched -- the 2026-08-05..19
    gap shape. Must be flagged against intraday_5m, not daily."""
    tip_date = date.today() - timedelta(days=100)
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [("NASDAQ", tip_date.isoformat(), 1, "GAP.US"),
                               ("NASDAQ", tip_date.isoformat(), 2, "OK.US")])
        _seed_full_daily_and_intraday(db, "GAP.US", tip_date, intraday=False)
        _seed_full_daily_and_intraday(db, "OK.US", tip_date, intraday=True)

        with caplog.at_level("INFO"):
            _log_unresolved_tips(db)

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "intraday_5m" in warnings[0] and "GAP.US" in warnings[0]
    assert "OK.US" not in warnings[0]
    assert any("no unresolved tips in daily" in r.message for r in caplog.records)


def test_log_unresolved_tips_intraday_skips_daily_resolved_outcomes(tmp_path, caplog):
    """A tip whose outcome was resolved from daily bars (EODHD has no
    intraday for it, e.g. a closed-end fund) is expected to be short in
    intraday_5m -- counted, not listed as a problem."""
    tip_date = date.today() - timedelta(days=100)
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [("NYSE", tip_date.isoformat(), 1, "CEF.US")])
        _seed_full_daily_and_intraday(db, "CEF.US", tip_date, intraday=False)
        db.conn.execute("CREATE TABLE outcomes (code TEXT, tip_date TEXT, fill_granularity TEXT)")
        db.conn.execute("INSERT INTO outcomes VALUES ('CEF.US', ?, '1d')", (tip_date.isoformat(),))
        db.conn.commit()

        with caplog.at_level("INFO"):
            _log_unresolved_tips(db)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no unresolved tips in intraday_5m (1 more resolved from daily bars"
               in r.message for r in caplog.records)


def test_log_unresolved_tips_logs_info_when_nothing_to_report(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        with caplog.at_level("INFO"):
            _log_unresolved_tips(db)

    assert any("no unresolved tips" in rec.message for rec in caplog.records)


def _seed_stale_ticker_db(
    db: Database, tips: list[tuple[str, str]], daily: list[tuple[str, str]]
) -> None:
    db.conn.execute(
        "CREATE TABLE tip_details (exchange TEXT, tip_date TEXT, tip_n INTEGER, code TEXT, "
        "entry_zone_low REAL, entry_zone_high REAL)"
    )
    db.conn.executemany(
        "INSERT INTO tip_details VALUES ('NASDAQ', ?, ?, ?, 7.08, 7.22)",
        [(tip_date, n, code) for n, (code, tip_date) in enumerate(tips, 1)],
    )
    _seed_daily_dates(db, daily)


def test_log_stale_tickers_flags_tip_with_no_recent_history(tmp_path, caplog):
    """ELWS shape: the newsletter tipped a symbol EODHD has no bars for
    around the tip date (renamed months earlier). Flagged a week after the
    tip -- without waiting for the window to close -- with the entry zone."""
    with Database(tmp_path / "test.db") as db:
        _seed_stale_ticker_db(
            db,
            [("STALE.US", "2026-05-15"), ("OK.US", "2026-05-15"),
             ("OLDDATA.US", "2026-05-15")],
            [("OK.US", "2026-05-14"),
             ("OLDDATA.US", "2026-01-16")],  # history, but long before the tip
        )
        with caplog.at_level("INFO"):
            _log_stale_tickers(db, today=date(2026, 5, 22))

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "STALE.US (tip 2026-05-15, entry 7.08-7.22)" in warnings[0]
    assert "OLDDATA.US" in warnings[0]
    assert "OK.US" not in warnings[0]


def test_log_stale_tickers_waits_for_first_daily_fetch(tmp_path, caplog):
    """A tip younger than STALE_TICKER_MIN_AGE_DAYS may simply not have
    had its daily-price run yet -- don't flag it."""
    with Database(tmp_path / "test.db") as db:
        _seed_stale_ticker_db(db, [("NEW.US", "2026-05-15")], [])
        with caplog.at_level("INFO"):
            _log_stale_tickers(db, today=date(2026, 5, 21))

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no stale-ticker tips" in r.message for r in caplog.records)


def test_log_stale_tickers_skips_already_aliased(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        _seed_stale_ticker_db(db, [("OLDSYM.US", "2026-05-15")], [])
        db.conn.execute(
            "CREATE TABLE ticker_aliases (old_code TEXT, new_code TEXT, effective_date TEXT)")
        db.conn.execute(
            "INSERT INTO ticker_aliases VALUES ('OLDSYM.US', 'NEWSYM.US', '2026-01-20')")
        with caplog.at_level("INFO"):
            _log_stale_tickers(db, today=date(2026, 5, 22))

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_log_stale_tickers_missing_tables_is_silent(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        with caplog.at_level("INFO"):
            _log_stale_tickers(db)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


_OPEN_TS = 1779370200  # a 13:30 UTC market open; days below are offsets from it


def _seed_dead_intraday_db(
    db: Database, tips: list[tuple[str, str]],
    days: list[tuple[str, str, int, list[int]]],
) -> None:
    """days: (code, date, daily vo, 5m bar volumes for that day)."""
    db.conn.execute("CREATE TABLE tip_details (tip_date TEXT, code TEXT)")
    db.conn.executemany("INSERT INTO tip_details VALUES (?, ?)",
                        [(d, c) for c, d in tips])
    db.conn.execute("CREATE TABLE daily (code TEXT, date TEXT, timestamp INTEGER, vo INTEGER)")
    db.conn.execute("CREATE TABLE intraday_5m (code TEXT, timestamp INTEGER, vo INTEGER)")
    for n, (code, day, daily_vo, bar_vols) in enumerate(days):
        ts = _OPEN_TS + n * 86400
        db.conn.execute("INSERT INTO daily VALUES (?, ?, ?, ?)", (code, day, ts, daily_vo))
        db.conn.executemany("INSERT INTO intraday_5m VALUES (?, ?, ?)",
                            [(code, ts + i * 300, v) for i, v in enumerate(bar_vols)])


def test_log_dead_intraday_flags_empty_5m_days_when_daily_traded(tmp_path, caplog):
    """SBLX/RVYL shape: after a rename EODHD keeps serving the old symbol's
    daily bars, but its 5m bars are all zero volume."""
    with Database(tmp_path / "test.db") as db:
        _seed_dead_intraday_db(
            db, [("OLD.US", "2026-05-20"), ("OK.US", "2026-05-20")],
            [("OLD.US", "2026-05-21", 46000, [500, 700]),
             ("OLD.US", "2026-05-22", 101700, [0, 0]),
             ("OLD.US", "2026-05-26", 32200, [0, 0]),
             ("OK.US", "2026-05-22", 90000, [0, 400])],  # one real bar is enough
        )
        with caplog.at_level("INFO"):
            _log_dead_intraday(db)

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "OLD.US (2d, 2026-05-22..2026-05-26, tips 2026-05-20)" in warnings[0]
    assert "OK.US" not in warnings[0]


def test_log_dead_intraday_ignores_thin_tickers_and_days_outside_windows(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        _seed_dead_intraday_db(
            db, [("THIN.US", "2026-05-20"), ("LATER.US", "2026-05-20")],
            [("THIN.US", "2026-05-22", 600, [0, 0]),          # MKDW/RDAC: barely trades
             ("LATER.US", "2026-08-20", 50000, [0, 0]),       # past the tip's window
             ("NOTIP.US", "2026-05-22", 50000, [0, 0])],      # no tip at all
        )
        with caplog.at_level("INFO"):
            _log_dead_intraday(db)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no dead intraday_5m days" in r.message for r in caplog.records)


def test_log_dead_intraday_ignores_days_with_no_5m_rows(tmp_path, caplog):
    """A day missing from intraday_5m entirely is a coverage gap, which
    _log_unresolved_tips() reports -- not this check's job."""
    with Database(tmp_path / "test.db") as db:
        _seed_dead_intraday_db(db, [("GAP.US", "2026-05-20")],
                               [("GAP.US", "2026-05-22", 50000, [])])
        with caplog.at_level("INFO"):
            _log_dead_intraday(db)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_log_dead_intraday_missing_tables_is_silent(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        with caplog.at_level("INFO"):
            _log_dead_intraday(db)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def _run_main_with_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["daily_update.py", *argv])
    with pytest.raises(SystemExit):
        main()


def test_main_backfill_requires_start_and_end_date(monkeypatch, tmp_path):
    _run_main_with_argv(
        monkeypatch,
        [
            "--db-path", str(tmp_path / "x.db"), "--eml-dir", str(tmp_path / "eml"),
            "--mode", "backfill",
        ],
    )


def test_main_backfill_requires_end_date_too(monkeypatch, tmp_path):
    _run_main_with_argv(
        monkeypatch,
        [
            "--db-path", str(tmp_path / "x.db"), "--eml-dir", str(tmp_path / "eml"),
            "--mode", "backfill", "--start-date", "2026-04-08",
        ],
    )


def test_main_backfill_rejects_all_emails(monkeypatch, tmp_path):
    _run_main_with_argv(
        monkeypatch,
        [
            "--db-path", str(tmp_path / "x.db"), "--eml-dir", str(tmp_path / "eml"),
            "--mode", "backfill", "--start-date", "2026-04-08", "--end-date", "2026-04-21",
            "--all-emails",
        ],
    )


def test_main_non_backfill_rejects_start_date(monkeypatch, tmp_path):
    _run_main_with_argv(
        monkeypatch,
        [
            "--db-path", str(tmp_path / "x.db"), "--eml-dir", str(tmp_path / "eml"),
            "--mode", "tips-only", "--start-date", "2026-04-08",
        ],
    )
