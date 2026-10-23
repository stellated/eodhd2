"""Offline tests for scripts/eodhd_budget.py, on a hand-built alphanume.db."""
import json
from datetime import date

import pytest

from scripts import eodhd_budget as eb
from scripts import get_alphanume as ga


@pytest.fixture
def conn(tmp_path):
    return ga.connect(tmp_path / "alphanume.db")


@pytest.fixture(scope="module")
def cal():
    return eb.calendar()


def _store(conn, job, rows, completed=True, capture="history"):
    wid = conn.execute(
        "INSERT INTO windows (job, params, capture, started_at, completed_at) VALUES (?,?,?,?,?)",
        (job, "{}", capture, "x", "y" if completed else None)).lastrowid
    conn.execute(
        "INSERT INTO raw_responses (window_id, endpoint, page, page_params, fetched_at, "
        "http_status, row_count, body) VALUES (?,?,?,?,?,?,?,?)",
        (wid, "e", 0, "{}", "x", 200, len(rows), json.dumps({"data": rows})))
    conn.commit()


def test_eodhd_code_maps_share_class_dot():
    assert eb.eodhd_code("brk.b") == "BRK-B.US"


def test_ndm_event_is_the_next_session(conn, cal):
    # picked Thursday 2026-07-02; Friday 07-03 is a holiday, so the trade is Monday 07-06
    _store(conn, "ndm", [{"date": "2026-07-02", "ticker": "AAPL"}])
    assert eb.events(conn, "ndm", cal) == ([("AAPL.US", date(2026, 7, 6))], 0)


def test_events_use_date_field_fallbacks_and_count_skips(conn, cal):
    _store(conn, "dilution", [{"filing_date": "2026-03-04T00:00:00", "ticker": "X"},
                              {"date": "2026-03-05", "ticker": "Y"},
                              {"ticker": "Z"},
                              {"filing_date": "2026-03-06"}])
    evs, skipped = eb.events(conn, "dilution", cal)
    assert evs == [("X.US", date(2026, 3, 4)), ("Y.US", date(2026, 3, 5))]
    assert skipped == 2


def test_events_ignore_incomplete_and_live_windows(conn, cal):
    _store(conn, "dilution", [{"date": "2026-03-05", "ticker": "A"}], completed=False)
    _store(conn, "dilution", [{"date": "2026-03-05", "ticker": "B"}], capture="live")
    assert eb.events(conn, "dilution", cal) == ([], 0)


def test_spans_respect_max_length():
    days = [date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 12), date(2026, 1, 20)]
    assert eb.spans(days, 10) == [(date(2026, 1, 1), date(2026, 1, 3)),
                                  (date(2026, 1, 12), date(2026, 1, 20))]


def test_merge_intervals_joins_adjacent_and_overlapping():
    got = eb.merge_intervals([(date(2026, 1, 5), date(2026, 1, 9)),
                              (date(2026, 1, 1), date(2026, 1, 4)),
                              (date(2026, 2, 1), date(2026, 2, 2))])
    assert got == [(date(2026, 1, 1), date(2026, 1, 9)), (date(2026, 2, 1), date(2026, 2, 2))]


def test_plan_counts_calls_and_rows(cal):
    # two NDM trade days for one ticker, 2 weeks apart: 1 daily request,
    # 2 intraday requests per event but 1 merged span
    evs = [("AAPL.US", date(2026, 3, 2)), ("AAPL.US", date(2026, 3, 16))]
    p = eb.plan({"ndm": evs}, cal)
    assert (p["daily_requests"], p["intraday_requests_per_event"],
            p["intraday_requests_merged"]) == (1, 2, 1)
    assert p["calls_per_event"] == 1 + 2 * 5 and p["calls_merged"] == 1 + 5
    assert p["intraday_rows"] == 2 * eb.BARS_PER_DAY["5m"]
    # daily windows 01-31..03-02 and 02-14..03-16 merge into 01-31..03-16
    assert p["daily_rows"] == len(cal.sessions_in_range("2026-01-31", "2026-03-16"))


def test_ndm_before_5m_history_uses_1m_stored_as_5m(cal):
    p = eb.plan({"ndm": [("AAPL.US", date(2019, 6, 3))]}, cal)
    (req,) = [r for r in p["fetch_list"] if r["kind"] != "daily"]
    assert req["kind"] == "1m"
    assert p["intraday_rows"] == eb.BARS_PER_DAY["5m"]


def test_tickers_shared_by_jobs_are_fetched_once(cal):
    evs = {"ndm": [("AAPL.US", date(2026, 3, 2))],
           "premarket_drop": [("AAPL.US", date(2026, 3, 2))]}
    total = eb.plan(evs, cal)
    kinds = sorted(r["kind"] for r in total["fetch_list"])
    assert kinds == ["1m", "5m", "daily"]


def test_plan_flags_clipped_daily_and_unchecked_1m(cal):
    p = eb.plan({"premarket_drop": [("X.US", date(2006, 7, 10))]}, cal)
    assert p["daily_sessions_clipped"] > 0 and p["unchecked_1m_days"] == 1


def test_reverse_splits_add_a_splits_request(cal):
    p = eb.plan({"reverse_splits": [("X.US", date(2026, 3, 2))]}, cal)
    assert p["splits_requests"] == 1 and p["calls_merged"] == 2


def test_report_lists_tickers_missing_from_symbol_lists(conn, tmp_path):
    _store(conn, "dilution", [{"date": "2026-03-05", "ticker": "AAA"},
                              {"date": "2026-03-06", "ticker": "ZZZ"}])
    sym = tmp_path / "US.csv"
    sym.write_text("Code,Name,Country,Exchange,Currency,Type,Isin\n"
                   "AAA,A Inc,USA,NYSE,USD,Common Stock,\n")
    rows, total = eb.report(conn, eb.read_symbols([sym]))
    ((job, _, _, unknown, _),) = rows
    assert (job, unknown) == ("dilution", ["ZZZ.US"])
    assert total["tickers"] == 2
