from datetime import date, timedelta

from scripts.daily_update import _all_tips, _log_unresolved_tips, _still_open_tips
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


def test_log_unresolved_tips_logs_info_when_nothing_to_report(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        with caplog.at_level("INFO"):
            _log_unresolved_tips(db)

    assert any("no unresolved tips" in rec.message for rec in caplog.records)
