"""Tests for the outcomes table: the trigger-crossing primitives, the
per-status checkers, chaining, seeding/dedup, and a couple of
end-to-end update_outcomes() runs against a real (temp) Database.
"""
from datetime import date

import pandas as pd
import polars as pl
import pytest

from src.eodhd_io import (
    Database,
    _advance_position,
    _apply_slippage,
    _check_open,
    _check_pending,
    _entry_window_closed,
    _level_crossed,
    _load_tip,
    _seed_new_outcomes,
    _sell_fields,
    _time_exit_due,
    _zone_touched,
    pandas2sqlite,
    update_outcomes,
)


def _bar(timestamp, local_date, local_time, op, hi, lo, cl, vo=1000) -> dict:
    """A single bar as the plain dict _level_crossed/_zone_touched/the
    checkers expect (mirrors one row of an intraday_5m-shaped DataFrame)."""
    return {
        "timestamp": timestamp, "local_date": local_date, "local_time": local_time,
        "op": op, "hi": hi, "lo": lo, "cl": cl, "vo": vo,
    }


def _bars(rows: list[dict]) -> pl.DataFrame:
    df = pl.DataFrame(rows)
    return df.with_columns(pl.col("local_time").str.to_time("%H:%M:%S"))


def _tip(**overrides) -> dict:
    tip = {
        "tip_date": date(2026, 1, 5),
        "entry_zone_low": 10.0,
        "entry_zone_high": 10.5,
        "target": 12.0,
        "stop": 9.0,
        "holding_period_low": 2,
        "holding_period_high": 5,
    }
    tip.update(overrides)
    return tip


def _pos(**overrides) -> dict:
    pos = {
        "code": "AAPL.US", "tip_date": date(2026, 1, 5), "exchange": "NASDAQ",
        "tip_n": 1, "status": "pending",
        "buy_date": None, "buy_timestamp": None, "buy_local_time": None,
        "buy_trigger_price": None, "buy_price": None, "buy_bar_volume": None,
        "risk_per_share": None,
        "sell_date": None, "sell_timestamp": None, "sell_local_time": None,
        "sell_trigger_price": None, "sell_price": None, "sell_bar_volume": None,
        "profit_per_share": None, "r_multiple": None,
    }
    pos.update(overrides)
    return pos


# --- _level_crossed -------------------------------------------------------

def test_level_crossed_up_not_reached():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 10.0, 10.3, 9.9, 10.1)
    assert _level_crossed(bar, 12.0, "up") == (False, None)

def test_level_crossed_up_mid_bar_fills_at_level():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 10.0, 12.5, 9.9, 12.0)
    assert _level_crossed(bar, 12.0, "up") == (True, 12.0)

def test_level_crossed_up_gapped_fills_at_open():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 12.5, 13.0, 12.4, 12.8)
    assert _level_crossed(bar, 12.0, "up") == (True, 12.5)

def test_level_crossed_down_not_reached():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 10.0, 10.3, 9.9, 10.1)
    assert _level_crossed(bar, 9.0, "down") == (False, None)

def test_level_crossed_down_mid_bar_fills_at_level():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 10.0, 10.1, 8.5, 9.0)
    assert _level_crossed(bar, 9.0, "down") == (True, 9.0)

def test_level_crossed_down_gapped_fills_at_open():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 8.5, 8.7, 8.3, 8.4)
    assert _level_crossed(bar, 9.0, "down") == (True, 8.5)


# --- _zone_touched ----------------------------------------------------

def test_zone_touched_entirely_above():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.2, 10.8, 11.0)
    assert _zone_touched(bar, 10.0, 10.5) == (False, None)

def test_zone_touched_entirely_below():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 9.0, 9.2, 8.8, 9.0)
    assert _zone_touched(bar, 10.0, 10.5) == (False, None)

def test_zone_touched_opens_inside():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 10.2, 10.4, 10.1, 10.3)
    assert _zone_touched(bar, 10.0, 10.5) == (True, 10.2)

def test_zone_touched_falling_from_above():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.3, 10.4)
    assert _zone_touched(bar, 10.0, 10.5) == (True, 10.5)

def test_zone_touched_rising_from_below():
    bar = _bar(1, date(2026, 1, 5), "09:30:00", 9.0, 10.2, 8.9, 10.1)
    assert _zone_touched(bar, 10.0, 10.5) == (True, 10.0)


# --- _apply_slippage --------------------------------------------------

def test_apply_slippage_buy_costs_more():
    assert _apply_slippage(100.0, "buy", bps=10) == pytest.approx(100.10)

def test_apply_slippage_sell_receives_less():
    assert _apply_slippage(100.0, "sell", bps=10) == pytest.approx(99.90)


# --- window/deadline boundaries ----------------------------------------

def test_entry_window_still_open_at_exactly_holding_period_low():
    tip = _tip(holding_period_low=2)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 1, 1, 1, 1),
        _bar(2, date(2026, 1, 6), "09:30:00", 1, 1, 1, 1),
        _bar(3, date(2026, 1, 7), "09:30:00", 1, 1, 1, 1),  # day 2 -- still OK
    ])
    assert _entry_window_closed(tip, bars) is False

def test_entry_window_closed_one_day_past_holding_period_low():
    tip = _tip(holding_period_low=2)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 1, 1, 1, 1),
        _bar(2, date(2026, 1, 6), "09:30:00", 1, 1, 1, 1),
        _bar(3, date(2026, 1, 7), "09:30:00", 1, 1, 1, 1),
        _bar(4, date(2026, 1, 8), "09:30:00", 1, 1, 1, 1),  # day 3 -- closed
    ])
    assert _entry_window_closed(tip, bars) is True

def test_time_exit_not_due_at_exactly_holding_period_high():
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5))
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 1, 1, 1, 1),
        _bar(2, date(2026, 1, 6), "09:30:00", 1, 1, 1, 1),  # day 1 -- still OK
    ])
    assert _time_exit_due(pos, tip, bars) is False

def test_time_exit_due_one_day_past_holding_period_high():
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5))
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 1, 1, 1, 1),
        _bar(2, date(2026, 1, 6), "09:30:00", 1, 1, 1, 1),
        _bar(3, date(2026, 1, 7), "09:30:00", 1, 1, 1, 1),  # day 2 -- due
    ])
    assert _time_exit_due(pos, tip, bars) is True


# --- _sell_fields -------------------------------------------------------

def test_sell_fields_computes_profit_and_r_multiple():
    pos = _pos(status="open", buy_price=10.10, risk_per_share=1.10)
    bar = _bar(5, date(2026, 1, 10), "11:00:00", 12.0, 12.1, 11.9, 12.0, vo=5000)
    fields = _sell_fields(bar, trigger_price=12.0, pos=pos)
    expected_sell_price = _apply_slippage(12.0, "sell")
    assert fields["sell_price"] == pytest.approx(expected_sell_price)
    assert fields["profit_per_share"] == pytest.approx(expected_sell_price - 10.10)
    assert fields["r_multiple"] == pytest.approx((expected_sell_price - 10.10) / 1.10)
    assert fields["sell_bar_volume"] == 5000


# --- _check_pending -------------------------------------------------------

def test_check_pending_enters_on_zone_touch():
    tip = _tip()
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),  # above zone
        _bar(2, date(2026, 1, 5), "09:35:00", 10.8, 10.9, 10.2, 10.3),  # falls into zone
    ])
    result = _check_pending(pos, bars, tip)
    assert result["status"] == "open"
    assert result["buy_trigger_price"] == 10.5  # entered from above -> zone_high
    assert result["buy_timestamp"] == 2
    assert result["risk_per_share"] == pytest.approx(result["buy_price"] - tip["stop"])

def test_check_pending_aborts_on_stop_breach_before_entry():
    tip = _tip()
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 8.8, 8.9, 8.5, 8.6),  # straight below stop
    ])
    result = _check_pending(pos, bars, tip)
    assert result["status"] == "aborted"

def test_check_pending_expires_unfilled_after_window_closes():
    tip = _tip(holding_period_low=1)
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),
        _bar(2, date(2026, 1, 6), "09:30:00", 11.2, 11.3, 11.1, 11.2),
        _bar(3, date(2026, 1, 7), "09:30:00", 11.4, 11.5, 11.3, 11.4),  # day 2 -- closed
    ])
    result = _check_pending(pos, bars, tip)
    assert result["status"] == "expired_unfilled"

def test_check_pending_stays_pending_if_window_still_open():
    tip = _tip(holding_period_low=5)
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),
    ])
    result = _check_pending(pos, bars, tip)
    assert result == pos

def test_check_pending_ignores_zero_volume_bar_touching_zone():
    # A padded/no-trade bar (vo=0) sits inside the entry zone -- must not
    # trigger entry. Only the later real-volume bar should.
    tip = _tip()
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.2, 10.3, 10.1, 10.2, vo=0),
        _bar(2, date(2026, 1, 5), "09:35:00", 10.8, 10.9, 10.2, 10.3, vo=500),
    ])
    result = _check_pending(pos, bars, tip)
    assert result["status"] == "open"
    assert result["buy_timestamp"] == 2

def test_check_pending_ignores_zero_volume_bar_breaching_stop():
    # A padded/no-trade bar (vo=0) dips below the stop -- must not abort.
    # Window stays open (holding_period_low generous) since no real bar
    # has triggered anything yet.
    tip = _tip(holding_period_low=5)
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 8.0, 8.1, 7.9, 8.0, vo=0),
    ])
    result = _check_pending(pos, bars, tip)
    assert result == pos


# --- _check_open ----------------------------------------------------------

def test_check_open_hits_target():
    tip = _tip()
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 5), "09:35:00", 11.9, 12.1, 11.8, 12.0),
    ])
    result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_target"
    assert result["sell_trigger_price"] == 12.0

def test_check_open_hits_stop():
    tip = _tip()
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 5), "09:35:00", 9.2, 9.3, 8.8, 8.9),
    ])
    result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_stop"
    assert result["sell_trigger_price"] == 9.0

def test_check_open_same_bar_collision_assumes_stop_and_warns(caplog):
    tip = _tip()
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 5), "09:35:00", 10.0, 12.5, 8.5, 10.0),  # spans both
    ])
    with caplog.at_level("WARNING"):
        result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_stop"
    assert any("both target and stop" in r.message for r in caplog.records)

def test_check_open_same_bar_as_entry_warns(caplog):
    tip = _tip()
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 12.1, 10.4, 12.0),  # entry bar also hits target
    ])
    with caplog.at_level("WARNING"):
        result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_target"
    assert any("same bar as entry" in r.message for r in caplog.records)

def test_check_open_time_exit_uses_last_close():
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 6), "09:30:00", 10.6, 10.8, 10.5, 10.7),
        _bar(3, date(2026, 1, 7), "16:00:00", 10.9, 11.0, 10.8, 10.95),  # day 2 -- due
    ])
    result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_time"
    assert result["sell_trigger_price"] == 10.95

def test_check_open_stays_open_if_nothing_triggers():
    tip = _tip(holding_period_high=5)
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
    ])
    result = _check_open(pos, bars, tip)
    assert result == pos

def test_check_open_ignores_zero_volume_bar_hitting_target():
    # A padded/no-trade bar (vo=0) spikes through target -- must not exit.
    tip = _tip()
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 5), "09:35:00", 11.9, 12.1, 11.8, 12.0, vo=0),
    ])
    result = _check_open(pos, bars, tip)
    assert result == pos

def test_check_open_time_exit_deferred_with_no_real_bar_since_buy():
    # Window has closed, but every bar since buy is zero-volume (an
    # extended halt) -- don't force a time-exit fill onto a padded/NULL
    # close; stay open and reconsider next call.
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5, vo=0),
        _bar(2, date(2026, 1, 6), "09:30:00", 10.5, 10.6, 10.4, 10.5, vo=0),
        _bar(3, date(2026, 1, 7), "09:30:00", 10.5, 10.6, 10.4, 10.5, vo=0),  # day 2 -- due
    ])
    result = _check_open(pos, bars, tip)
    assert result == pos

def test_check_open_time_exit_uses_last_real_close_not_padded_close():
    # The window's deadline bar itself is padded (vo=0, stale carried-
    # forward close) -- the time exit should fill at the last *real*
    # bar's close instead, not the padded one.
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
                buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 6), "09:30:00", 10.6, 10.8, 10.5, 10.65),
        _bar(3, date(2026, 1, 7), "16:00:00", 10.99, 10.99, 10.99, 10.99, vo=0),  # padded
    ])
    result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_time"
    assert result["sell_trigger_price"] == 10.65  # last real bar's close (bar 2), not bar 3's


# --- _advance_position chaining -----------------------------------------

def test_advance_position_chains_entry_and_exit_in_one_pass():
    """A position that both enters and exits within the same batch of bars
    should resolve fully in one _advance_position() call, not lag a cycle."""
    tip = _tip()
    pos = _pos()
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),  # above zone
        _bar(2, date(2026, 1, 5), "09:35:00", 10.8, 10.9, 10.2, 10.3),  # enters
        _bar(3, date(2026, 1, 5), "09:40:00", 11.0, 12.1, 10.9, 12.0),  # hits target
    ])
    result = _advance_position(pos, bars, tip)
    assert result["status"] == "closed_target"
    assert result["buy_timestamp"] == 2
    assert result["sell_timestamp"] == 3

def test_advance_position_open_checker_never_sees_pre_entry_bars():
    """The 'open' checker must only see bars from buy_timestamp onward --
    a target/stop level crossed before entry must not count."""
    tip = _tip()
    pos = _pos()
    bars = _bars([
        # before entry: dips to stop level, but we're not in yet
        _bar(1, date(2026, 1, 5), "09:30:00", 9.1, 9.2, 8.9, 9.0),
        # never actually touches the entry zone in this synthetic example,
        # so this only tests that a pre-entry stop-level dip while still
        # pending correctly aborts rather than being misread as an exit
    ])
    result = _advance_position(pos, bars, tip)
    assert result["status"] == "aborted"


# --- _seed_new_outcomes ---------------------------------------------------

def _seed_tip_details(db: Database, rows: list[tuple]) -> None:
    db.conn.execute("""
        CREATE TABLE tip_details (
            exchange TEXT, tip_date TEXT, tip_n INTEGER, code TEXT,
            entry_zone_low REAL, entry_zone_high REAL, target REAL, stop REAL,
            holding_period_low INTEGER, holding_period_high INTEGER
        )
    """)
    db.conn.executemany(
        "INSERT INTO tip_details (exchange, tip_date, tip_n, code) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.conn.commit()

def test_seed_new_outcomes_creates_one_pending_row_per_tip(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [("NASDAQ", "2026-01-05", 1, "AAA.US")])
        db.conn.execute(
            "CREATE TABLE outcomes (code TEXT, tip_date TEXT, exchange TEXT, "
            "tip_n INTEGER, status TEXT, PRIMARY KEY (code, tip_date))"
        )
        _seed_new_outcomes(db)
        rows = db.conn.execute("SELECT code, tip_date, status FROM outcomes").fetchall()
    assert rows == [("AAA.US", "2026-01-05", "pending")]

def test_seed_new_outcomes_dedupes_to_lowest_tip_n(tmp_path, caplog):
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [
            ("NYSE", "2026-01-05", 7, "DUP.US"),
            ("NASDAQ", "2026-01-05", 2, "DUP.US"),
        ])
        db.conn.execute(
            "CREATE TABLE outcomes (code TEXT, tip_date TEXT, exchange TEXT, "
            "tip_n INTEGER, status TEXT, PRIMARY KEY (code, tip_date))"
        )
        with caplog.at_level("WARNING"):
            _seed_new_outcomes(db)
        rows = db.conn.execute(
            "SELECT code, tip_date, exchange, tip_n FROM outcomes"
        ).fetchall()
    assert rows == [("DUP.US", "2026-01-05", "NASDAQ", 2)]
    assert any("tipped more than once" in r.message for r in caplog.records)

def test_seed_new_outcomes_skips_already_seeded_tips(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_tip_details(db, [("NASDAQ", "2026-01-05", 1, "AAA.US")])
        db.conn.execute(
            "CREATE TABLE outcomes (code TEXT, tip_date TEXT, exchange TEXT, "
            "tip_n INTEGER, status TEXT, PRIMARY KEY (code, tip_date))"
        )
        _seed_new_outcomes(db)
        _seed_new_outcomes(db)  # second call must not error or duplicate
        rows = db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()
    assert rows[0] == 1


# --- update_outcomes() end-to-end -----------------------------------------

def _seed_full_tip(db: Database, **overrides) -> None:
    tip = {
        "exchange": "NASDAQ", "tip_date": "2026-01-05", "tip_n": 1, "code": "AAA.US",
        "entry_zone_low": 10.0, "entry_zone_high": 10.5, "target": 12.0, "stop": 9.0,
        "holding_period_low": 2, "holding_period_high": 5,
    }
    tip.update(overrides)
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS tip_details (
            exchange TEXT, tip_date TEXT, tip_n INTEGER, code TEXT,
            entry_zone_low REAL, entry_zone_high REAL, target REAL, stop REAL,
            holding_period_low INTEGER, holding_period_high INTEGER
        )
    """)
    cols = ", ".join(tip.keys())
    placeholders = ", ".join(f":{c}" for c in tip)
    db.conn.execute(
        f"INSERT INTO tip_details ({cols}) VALUES ({placeholders})", tip  # noqa: S608
    )
    db.conn.commit()

def _seed_intraday_bars(db: Database, code: str, rows: list[dict]) -> None:
    pdf = pd.DataFrame([{
        "code": code,
        "timestamp": r["timestamp"],
        "datetime": pd.Timestamp(r["local_date"]),
        "local_date": r["local_date"],
        "local_time": r["local_time"],
        "op": r["op"], "hi": r["hi"], "lo": r["lo"], "cl": r["cl"], "vo": r["vo"],
    } for r in rows])
    pandas2sqlite(pdf, db.conn, "intraday_5m")

def test_update_outcomes_end_to_end_closes_at_target(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db)
        _seed_intraday_bars(db, "AAA.US", [
            {"timestamp": 1, "local_date": date(2026, 1, 5), "local_time": "09:30:00",
             "op": 11.0, "hi": 11.1, "lo": 10.9, "cl": 11.0, "vo": 1000},
            {"timestamp": 2, "local_date": date(2026, 1, 5), "local_time": "09:35:00",
             "op": 10.8, "hi": 10.9, "lo": 10.2, "cl": 10.3, "vo": 2000},
            {"timestamp": 3, "local_date": date(2026, 1, 5), "local_time": "09:40:00",
             "op": 11.0, "hi": 12.1, "lo": 10.9, "cl": 12.0, "vo": 3000},
        ])
        update_outcomes(db)
        row = db.conn.execute(
            "SELECT status, buy_trigger_price, sell_trigger_price "
            "FROM outcomes WHERE code = 'AAA.US' AND tip_date = '2026-01-05'"
        ).fetchone()
    assert row == ("closed_target", 10.5, 12.0)

def test_update_outcomes_stays_pending_with_no_price_data_yet(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db)
        update_outcomes(db)
        row = db.conn.execute(
            "SELECT status FROM outcomes WHERE code = 'AAA.US' AND tip_date = '2026-01-05'"
        ).fetchone()
    assert row == ("pending",)

def test_update_outcomes_is_idempotent_on_resolved_positions(tmp_path):
    """Calling update_outcomes() again after a position has closed must not
    error or change the stored row."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db)
        _seed_intraday_bars(db, "AAA.US", [
            {"timestamp": 1, "local_date": date(2026, 1, 5), "local_time": "09:30:00",
             "op": 11.0, "hi": 11.1, "lo": 10.9, "cl": 11.0, "vo": 1000},
            {"timestamp": 2, "local_date": date(2026, 1, 5), "local_time": "09:35:00",
             "op": 10.8, "hi": 10.9, "lo": 10.2, "cl": 10.3, "vo": 2000},
            {"timestamp": 3, "local_date": date(2026, 1, 5), "local_time": "09:40:00",
             "op": 11.0, "hi": 12.1, "lo": 10.9, "cl": 12.0, "vo": 3000},
        ])
        update_outcomes(db)
        update_outcomes(db)
        row = db.conn.execute(
            "SELECT status FROM outcomes WHERE code = 'AAA.US' AND tip_date = '2026-01-05'"
        ).fetchone()
    assert row == ("closed_target",)


# --- _load_tip validation / update_outcomes() resilience to bad tip rows --

def test_load_tip_raises_naming_the_null_field(tmp_path):
    """A real incident: tips_io.py has been observed to leave
    holding_period_low/high NULL for some full-detail cards (tip_n<=3) --
    _load_tip() must name exactly which field(s) are missing, not let a
    None reach the trigger-crossing comparisons as an opaque TypeError."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db, holding_period_low=None, holding_period_high=None)
        with pytest.raises(ValueError, match="holding_period_low.*holding_period_high"):
            _load_tip(db, "NASDAQ", "2026-01-05", 1)

def test_update_outcomes_skips_bad_tip_and_continues(tmp_path, caplog):
    """A tip_details row with a NULL required field must not abort the
    whole update_outcomes() run -- same resilience philosophy as tips()'s
    per-tip try/except."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db, code="BAD.US", holding_period_low=None, holding_period_high=None)
        _seed_full_tip(db, code="GOOD.US", exchange="NYSE")
        _seed_intraday_bars(db, "GOOD.US", [
            {"timestamp": 1, "local_date": date(2026, 1, 5), "local_time": "09:30:00",
             "op": 11.0, "hi": 11.1, "lo": 10.9, "cl": 11.0, "vo": 1000},
            {"timestamp": 2, "local_date": date(2026, 1, 5), "local_time": "09:35:00",
             "op": 10.8, "hi": 10.9, "lo": 10.2, "cl": 10.3, "vo": 2000},
            {"timestamp": 3, "local_date": date(2026, 1, 5), "local_time": "09:40:00",
             "op": 11.0, "hi": 12.1, "lo": 10.9, "cl": 12.0, "vo": 3000},
        ])
        with caplog.at_level("ERROR"):
            update_outcomes(db)
        rows = dict(db.conn.execute(
            "SELECT code, status FROM outcomes"
        ).fetchall())
    assert rows == {"BAD.US": "pending", "GOOD.US": "closed_target"}
    assert any("BAD.US" in rec.message for rec in caplog.records)
