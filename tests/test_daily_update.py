from datetime import date

from scripts.daily_update import _still_open_tips
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
