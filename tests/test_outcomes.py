"""Tests for the outcomes table: the trigger-crossing primitives, the
per-status checkers, chaining, seeding/dedup, and a couple of
end-to-end update_outcomes() runs against a real (temp) Database.
"""
from datetime import date, timedelta

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
    _load_bars_with_fallback,
    _load_tip,
    _seed_new_outcomes,
    _sell_fields,
    _set_intraday_unavailable,
    _split_adjust,
    _splits_after,
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


def test_check_pending_bar_opening_below_stop_aborts_even_if_it_reaches_zone():
    # Gapped below the stop, then rallied into the zone: the setup was
    # broken before any trade, so no entry.
    tip = _tip()
    bars = _bars([_bar(1, date(2026, 1, 5), "09:30:00", 8.8, 10.2, 8.5, 10.1)])
    assert _check_pending(_pos(), bars, tip)["status"] == "aborted"


@pytest.mark.parametrize("op, hi, expected_same_bar", [
    (10.2, 10.3, "entry_stop"),          # opened inside the zone: certain entry (WS.US)
    (11.0, 11.0, "entry_stop"),          # fell from above through the zone
    (9.5, 10.2, "entry_stop"),           # opened between stop and zone
    (10.2, 12.5, "entry_target_stop"),   # covered all three levels
])
def test_bar_touching_zone_and_stop_enters_then_stops_out(op, hi, expected_same_bar):
    """A bar reaching both the entry zone and the stop (without opening
    below the stop) is an entry stopped out on the same bar -- worst case,
    not 'aborted' at 0R."""
    tip = _tip()
    bars = _bars([_bar(1, date(2026, 1, 5), "09:30:00", op, hi, 8.5, 9.0)])
    result = _advance_position(_pos(), bars, tip)
    assert result["status"] == "closed_stop"
    assert result["same_bar"] == expected_same_bar
    assert result["buy_timestamp"] == result["sell_timestamp"] == 1
    assert result["sell_trigger_price"] == 9.0
    assert result["r_multiple"] < -1.0  # a full stop-out plus slippage both legs


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
    assert result["same_bar"] is None

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
    assert result["same_bar"] == "target_stop"
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
    assert result["same_bar"] == "entry_target"
    assert any("same bar as entry" in r.message for r in caplog.records)

def test_check_open_time_exit_sells_at_deadline_day_close():
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
    # holding_period_high=1: held through day 1, so day 1's close --
    # day 2's bar only shows the deadline has passed.
    assert result["sell_trigger_price"] == 10.7
    assert result["sell_timestamp"] == 2

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


def test_check_pending_ignores_entry_after_window():
    """ALGM shape: bars from a later tip's fetch touch the zone months
    after this tip's entry window closed -- that's expired_unfilled, not an
    entry."""
    tip = _tip(holding_period_low=1)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),
        _bar(2, date(2026, 1, 6), "09:30:00", 11.2, 11.3, 11.1, 11.2),
        _bar(3, date(2026, 4, 1), "09:30:00", 10.3, 10.4, 10.2, 10.3),  # in zone, too late
    ])
    assert _check_pending(_pos(), bars, tip)["status"] == "expired_unfilled"


def test_check_pending_ignores_stop_breach_after_window():
    tip = _tip(holding_period_low=1)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 11.0, 11.1, 10.9, 11.0),
        _bar(2, date(2026, 1, 6), "09:30:00", 11.2, 11.3, 11.1, 11.2),
        _bar(3, date(2026, 1, 7), "09:30:00", 8.8, 8.9, 8.5, 8.6),  # below stop, day 2
    ])
    assert _check_pending(_pos(), bars, tip)["status"] == "expired_unfilled"


def test_check_open_ignores_target_after_deadline():
    """TSLA shape: with more data in the table than the holding period,
    a target hit after the deadline must not count, and the time exit is
    the deadline day's close, not the newest bar's."""
    tip = _tip(holding_period_high=1)
    pos = _pos(status="open", buy_date=date(2026, 1, 5), buy_timestamp=1,
               buy_price=10.5, risk_per_share=1.5)
    bars = _bars([
        _bar(1, date(2026, 1, 5), "09:30:00", 10.5, 10.6, 10.4, 10.5),
        _bar(2, date(2026, 1, 6), "15:55:00", 10.6, 10.8, 10.5, 10.7),
        _bar(3, date(2026, 1, 7), "09:30:00", 11.9, 12.5, 11.8, 12.4),  # target, too late
        _bar(4, date(2026, 1, 30), "15:55:00", 13.0, 13.1, 12.9, 13.0),
    ])
    result = _check_open(pos, bars, tip)
    assert result["status"] == "closed_time"
    assert result["sell_timestamp"] == 2
    assert result["sell_trigger_price"] == 10.7


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


# --- daily-bar outcomes fallback (tickers EODHD never has intraday for) ---

def _seed_daily_bars(db: Database, code: str, rows: list[dict]) -> None:
    pdf = pd.DataFrame([{
        "code": code,
        "timestamp": r["timestamp"],
        "datetime": pd.Timestamp(r["date"]),
        "date": r["date"],
        "op": r["op"], "hi": r["hi"], "lo": r["lo"], "cl": r["cl"],
        "ac": r.get("ac", r["cl"]), "vo": r["vo"],
    } for r in rows])
    pandas2sqlite(pdf, db.conn, "daily")

# A tip old enough that its window (n2*2+5 = 45 calendar days after
# tip_date, default n2=20) has definitely closed, regardless of when the
# test suite actually runs.
_OLD_TIP_DATE = date.today() - timedelta(days=200)
_RECENT_TIP_DATE = date.today() - timedelta(days=5)

def test_load_bars_with_fallback_uses_daily_when_no_intraday_ever_and_window_closed(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _set_intraday_unavailable(db, "CEF.US", _OLD_TIP_DATE, "intraday_5m", True)
        _seed_daily_bars(db, "CEF.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
            {"timestamp": 2, "date": _OLD_TIP_DATE + timedelta(days=1),
             "op": 10.1, "hi": 10.3, "lo": 10.0, "cl": 10.2, "vo": 1100},
        ])
        bars, granularity = _load_bars_with_fallback(
            db, "CEF.US", from_date=_OLD_TIP_DATE, tip_date=_OLD_TIP_DATE
        )
    assert granularity == "1d"
    assert len(bars) == 2
    assert "local_date" in bars.columns
    assert "local_time" in bars.columns
    assert bars["local_time"].to_list() == [None, None]

def test_load_bars_with_fallback_prefers_intraday_when_available(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "BOTH.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        _seed_intraday_bars(db, "BOTH.US", [
            {"timestamp": 1, "local_date": _OLD_TIP_DATE, "local_time": "09:30:00",
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        bars, granularity = _load_bars_with_fallback(
            db, "BOTH.US", from_date=_OLD_TIP_DATE, tip_date=_OLD_TIP_DATE
        )
    assert granularity == "5m"
    assert len(bars) == 1

def test_load_bars_with_fallback_waits_when_window_still_open(tmp_path):
    """A brand-new tip with no intraday bars yet shouldn't fall back to
    daily prematurely -- intraday might still arrive via the normal weekly
    fetch, and guessing early would permanently mislabel it '1d'."""
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "NEW.US", [
            {"timestamp": 1, "date": _RECENT_TIP_DATE,
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        bars, granularity = _load_bars_with_fallback(
            db, "NEW.US", from_date=_RECENT_TIP_DATE, tip_date=_RECENT_TIP_DATE
        )
    assert granularity is None
    assert bars.is_empty()

def test_load_bars_with_fallback_does_not_substitute_daily_for_a_real_gap(tmp_path):
    """If intraday_5m has rows for this code somewhere (just not from
    from_date onward), that's a genuine coverage gap, not "never had
    intraday" -- must not be papered over with daily data."""
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "GAP.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        _seed_intraday_bars(db, "GAP.US", [
            {"timestamp": 1, "local_date": _OLD_TIP_DATE - timedelta(days=30),
             "local_time": "09:30:00",
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        bars, granularity = _load_bars_with_fallback(
            db, "GAP.US", from_date=_OLD_TIP_DATE, tip_date=_OLD_TIP_DATE
        )
    assert granularity is None
    assert bars.is_empty()

def test_update_outcomes_end_to_end_resolves_via_daily_fallback(tmp_path):
    """A CEF-like ticker: full daily coverage, zero intraday ever, window
    long closed -- must resolve (not stay pending forever) and be tagged
    '1d' so a later stats pass can tell it apart from a 5m-resolved row."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db, code="CEF.US", tip_date=_OLD_TIP_DATE.isoformat())
        _set_intraday_unavailable(db, "CEF.US", _OLD_TIP_DATE, "intraday_5m", True)
        _seed_daily_bars(db, "CEF.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 11.0, "hi": 11.1, "lo": 10.9, "cl": 11.0, "vo": 1000},
            {"timestamp": 2, "date": _OLD_TIP_DATE + timedelta(days=1),
             "op": 10.8, "hi": 10.9, "lo": 10.2, "cl": 10.3, "vo": 2000},
            {"timestamp": 3, "date": _OLD_TIP_DATE + timedelta(days=2),
             "op": 11.0, "hi": 12.1, "lo": 10.9, "cl": 12.0, "vo": 3000},
        ])
        update_outcomes(db)
        row = db.conn.execute(
            "SELECT status, buy_trigger_price, sell_trigger_price, fill_granularity "
            "FROM outcomes WHERE code = 'CEF.US'"
        ).fetchone()
    assert row == ("closed_target", 10.5, 12.0, "1d")

def test_load_bars_with_fallback_needs_eodhd_confirmation_not_just_empty_table(tmp_path):
    """The 2026-08-05..19 case: zero intraday rows ever, window closed, but
    only because nobody fetched it -- EODHD never said it had nothing. Must
    not fall back to daily (that wrongly resolved ~100 blue-chip tips)."""
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "KLAC.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 10.0, "hi": 10.2, "lo": 9.9, "cl": 10.1, "vo": 1000},
        ])
        bars, granularity = _load_bars_with_fallback(
            db, "KLAC.US", from_date=_OLD_TIP_DATE, tip_date=_OLD_TIP_DATE
        )
    assert granularity is None
    assert bars.is_empty()

def test_update_outcomes_reopens_daily_resolved_row_once_intraday_arrives(tmp_path):
    """A '1d'-resolved row is recomputed at 5m as soon as intraday_5m has
    bars for its window -- terminal rows are otherwise never revisited."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db, code="LATE.US", tip_date=_OLD_TIP_DATE.isoformat())
        _set_intraday_unavailable(db, "LATE.US", _OLD_TIP_DATE, "intraday_5m", True)
        # Daily: stop hit on day 1 (after entering on day 0).
        _seed_daily_bars(db, "LATE.US", [
            {"timestamp": 1, "date": _OLD_TIP_DATE,
             "op": 11.0, "hi": 11.1, "lo": 10.4, "cl": 10.5, "vo": 1000},
            {"timestamp": 2, "date": _OLD_TIP_DATE + timedelta(days=1),
             "op": 10.5, "hi": 10.6, "lo": 8.5, "cl": 8.7, "vo": 2000},
        ])
        update_outcomes(db)
        first = db.conn.execute(
            "SELECT status, fill_granularity FROM outcomes WHERE code = 'LATE.US'"
        ).fetchone()

        # Intraday turns up later: target hit after entry, never the stop.
        _seed_intraday_bars(db, "LATE.US", [
            {"timestamp": 10, "local_date": _OLD_TIP_DATE, "local_time": "09:30:00",
             "op": 11.0, "hi": 11.1, "lo": 10.4, "cl": 10.5, "vo": 1000},
            {"timestamp": 11, "local_date": _OLD_TIP_DATE, "local_time": "09:35:00",
             "op": 10.5, "hi": 12.2, "lo": 10.5, "cl": 12.1, "vo": 1000},
        ])
        update_outcomes(db)
        second = db.conn.execute(
            "SELECT status, fill_granularity FROM outcomes WHERE code = 'LATE.US'"
        ).fetchone()
    assert first == ("closed_stop", "1d")
    assert second == ("closed_target", "5m")

def test_update_outcomes_migration_tags_preexisting_resolved_rows_as_5m(tmp_path):
    """A row resolved before fill_granularity existed was necessarily
    resolved against intraday_5m (the daily fallback didn't exist yet) --
    the migration must retroactively tag it, so a query that filters on
    fill_granularity doesn't silently lose all pre-existing history."""
    with Database(tmp_path / "test.db") as db:
        db.conn.execute("""
            CREATE TABLE tip_details (
                exchange TEXT, tip_date TEXT, tip_n INTEGER, code TEXT,
                entry_zone_low REAL, entry_zone_high REAL, target REAL, stop REAL,
                holding_period_low INTEGER, holding_period_high INTEGER
            )
        """)
        db.conn.execute("""
            CREATE TABLE outcomes (
                code TEXT NOT NULL, tip_date TEXT NOT NULL, exchange TEXT NOT NULL,
                tip_n INTEGER NOT NULL, status TEXT NOT NULL,
                buy_date TEXT, buy_timestamp INTEGER, buy_local_time TEXT,
                buy_trigger_price REAL, buy_price REAL, buy_bar_volume INTEGER,
                risk_per_share REAL, sell_date TEXT, sell_timestamp INTEGER,
                sell_local_time TEXT, sell_trigger_price REAL, sell_price REAL,
                sell_bar_volume INTEGER, profit_per_share REAL, r_multiple REAL,
                PRIMARY KEY (code, tip_date)
            )
        """)
        db.conn.execute(
            "INSERT INTO outcomes (code, tip_date, exchange, tip_n, status) "
            "VALUES ('OLD.US', '2026-01-01', 'NYSE', 1, 'closed_target')"
        )
        db.conn.commit()
        update_outcomes(db)
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(outcomes)")}
        granularity = db.conn.execute(
            "SELECT fill_granularity FROM outcomes WHERE code = 'OLD.US'"
        ).fetchone()[0]
    assert "fill_granularity" in cols
    assert "same_bar" in cols
    assert granularity == "5m"


# --- split adjustment (CVNA 2026-04-28: a 5-for-1 read as a -8.43R stop) ---

def _daily(d, cl, ac, vo=1000):
    return {"timestamp": int(pd.Timestamp(d).timestamp()), "date": d,
            "op": cl, "hi": cl, "lo": cl, "cl": cl, "ac": ac, "vo": vo}


def test_splits_after_finds_forward_split(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "CVNA.US", [
            _daily(date(2026, 5, 6), 389.38, 77.876),
            _daily(date(2026, 5, 7), 400.02, 80.004),
            _daily(date(2026, 5, 8), 77.94, 77.94),
        ])
        splits = _splits_after(db, "CVNA.US", date(2026, 4, 28))
    assert [(d, round(f, 6)) for d, f in splits] == [(date(2026, 5, 8), 5.0)]


def test_splits_after_finds_reverse_split(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "NVVE.US", [
            _daily(date(2026, 7, 2), 0.32, 5.76),
            _daily(date(2026, 7, 6), 4.89, 4.89),
        ])
        splits = _splits_after(db, "NVVE.US", date(2026, 7, 1))
    assert [(d, round(f, 6)) for d, f in splits] == [(date(2026, 7, 6), round(1 / 18, 6))]


def test_splits_after_ignores_ac_refetched_after_a_later_split(tmp_path):
    """Rows fetched at different times can have ac on different bases (a
    later split rewrote ac for one batch only); cl is continuous, so it's
    not a split inside the window."""
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "AAA.US", [
            _daily(date(2026, 1, 5), 100.0, 100.0),
            _daily(date(2026, 1, 6), 101.0, 50.5),
        ])
        assert _splits_after(db, "AAA.US", date(2026, 1, 2)) == []


def test_splits_after_ignores_dividends_and_splits_before_tip(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _seed_daily_bars(db, "AAA.US", [
            _daily(date(2026, 1, 2), 50.0, 25.0),
            _daily(date(2026, 1, 5), 25.0, 25.0),   # 2-for-1 before the tip
            _daily(date(2026, 1, 6), 24.5, 24.5),
            _daily(date(2026, 1, 7), 24.0, 24.24),  # ~1% dividend
        ])
        assert _splits_after(db, "AAA.US", date(2026, 1, 5)) == []


def test_split_adjust_scales_prices_from_split_date_only():
    bars = _bars([
        _bar(1, date(2026, 5, 7), "15:55:00", 400.0, 401.0, 399.0, 400.0, vo=10),
        _bar(2, date(2026, 5, 8), "09:30:00", 81.0, 82.0, 80.0, 81.5, vo=20),
    ])
    out = _split_adjust(bars, [(date(2026, 5, 8), 5.0)])
    assert out["cl"].to_list() == [400.0, 407.5]
    assert out["lo"].to_list() == [399.0, 400.0]
    assert out["vo"].to_list() == [10, 20]


def test_update_outcomes_split_inside_window_is_not_a_stop(tmp_path):
    """Bought at 10.5 (stop 9.0); a 2-for-1 next day halves the unadjusted
    price to ~5.3. Adjusted it's ~10.6, so the position stays open and later
    hits the 12.0 target (6.05 unadjusted)."""
    with Database(tmp_path / "test.db") as db:
        _seed_full_tip(db)
        _seed_daily_bars(db, "AAA.US", [
            _daily(date(2026, 1, 5), 10.6, 5.3),
            _daily(date(2026, 1, 6), 5.3, 5.3),
        ])
        _seed_intraday_bars(db, "AAA.US", [
            {"timestamp": 1, "local_date": date(2026, 1, 5), "local_time": "09:30:00",
             "op": 10.8, "hi": 10.9, "lo": 10.4, "cl": 10.6, "vo": 1000},
            {"timestamp": 2, "local_date": date(2026, 1, 6), "local_time": "09:30:00",
             "op": 5.3, "hi": 5.35, "lo": 5.25, "cl": 5.3, "vo": 1000},
            {"timestamp": 3, "local_date": date(2026, 1, 6), "local_time": "09:35:00",
             "op": 5.3, "hi": 6.05, "lo": 5.3, "cl": 6.0, "vo": 1000},
        ])
        update_outcomes(db)
        row = db.conn.execute(
            "SELECT status, buy_trigger_price, sell_trigger_price FROM outcomes "
            "WHERE code = 'AAA.US' AND tip_date = '2026-01-05'"
        ).fetchone()
    assert row == ("closed_target", 10.5, 12.0)
