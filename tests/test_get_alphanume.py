"""Offline tests for scripts/get_alphanume.py: a fake session stands in
for the API, answering from a function of (endpoint, params)."""
import json
from datetime import date

import pytest

from scripts import get_alphanume as ga


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.headers = headers or {}


class _Session:
    def __init__(self, respond):
        self.respond, self.calls, self.headers = respond, [], {}

    def get(self, url, params, timeout):
        endpoint = url.removeprefix(ga.BASE_URL + "/")
        self.calls.append((endpoint, dict(params)))
        return self.respond(endpoint, dict(params))


def _client(respond):
    session = _Session(respond)
    return ga.Client("KEY", session=session, sleep=lambda s: None, clock=lambda: 0.0), session


def _rows(n, d="2026-01-02", **extra):
    return [{"date": d, "ticker": f"T{i}", **extra} for i in range(n)]


@pytest.fixture
def conn(tmp_path):
    return ga.connect(tmp_path / "alphanume.db")


def test_client_sends_key_header():
    client, session = _client(lambda e, p: _Resp(200, {"count": 0, "data": []}))
    assert session.headers["X-API-Key"] == "KEY"


def test_client_retries_429_then_succeeds():
    answers = iter([_Resp(429, "slow down", {"Retry-After": "3"}),
                    _Resp(200, {"count": 0, "data": []})])
    slept = []
    session = _Session(lambda e, p: next(answers))
    client = ga.Client("KEY", session=session, sleep=slept.append, clock=lambda: 100.0)
    assert client.get("next-day-movers", {}) == (200, '{"count": 0, "data": []}')
    assert 3.0 in slept


def test_client_does_not_retry_403():
    client, session = _client(lambda e, p: _Resp(403, {"error": "PRO_SUBSCRIPTION_REQUIRED"}))
    status, _ = client.get("premarket-drop-risk", {})
    assert status == 403 and len(session.calls) == 1


def test_fetch_window_follows_next_cursor(conn):
    def respond(endpoint, params):
        if "cursor_date" not in params:
            return _Resp(200, {"count": 2, "has_more": True,
                               "next_cursor": {"date": "2026-01-05", "rank": 7},
                               "data": _rows(2)})
        assert (params["cursor_date"], params["cursor_rank"]) == ("2026-01-05", 7)
        return _Resp(200, {"count": 1, "has_more": False, "next_cursor": None,
                           "data": _rows(1, "2026-01-05")})
    client, session = _client(respond)
    _, rows = ga.fetch_window(conn, client, "premarket_drop",
                              {"date_gte": "2026-01-01", "date_lte": "2026-01-31"})
    assert rows == 3 and len(session.calls) == 2
    assert conn.execute("SELECT pages, row_count FROM windows").fetchone() == (2, 3)


def test_fetch_window_error_is_stored_and_window_left_incomplete(conn):
    client, _ = _client(lambda e, p: _Resp(400, {"error": "ticker required"}))
    with pytest.raises(ga.AlphanumeError, match="HTTP 400"):
        ga.fetch_window(conn, client, "dilution", {"date_gte": "2026-01-01"})
    assert conn.execute("SELECT http_status FROM raw_responses").fetchone() == (400,)
    assert conn.execute("SELECT completed_at FROM windows").fetchone() == (None,)


def test_pull_range_splits_possibly_truncated_unpaged_window(conn, monkeypatch):
    """next-day-movers isn't paged: a window with NONPAGED_MAX_ROWS rows may
    have been cut off, so it's halved; summary ignores the cut-off parent."""
    monkeypatch.setattr(ga, "NONPAGED_MAX_ROWS", 3)

    def respond(endpoint, params):
        n = 3 if params["date_gte"] == "2026-01-01" and params["date_lte"] == "2026-01-04" else 2
        return _Resp(200, {"count": n, "data": _rows(n, params["date_gte"])})
    client, session = _client(respond)
    ga.pull_range(conn, client, "ndm", date(2026, 1, 1), date(2026, 1, 4))
    assert [p["date_lte"] for _, p in session.calls] == ["2026-01-04", "2026-01-02", "2026-01-04"]
    (s,) = [r for r in ga.summary(conn) if r["capture"] == "history"]
    assert (s["rows"], s["pages"]) == (4, 2)


def test_pull_range_resumes_without_refetching_completed_windows(conn, monkeypatch):
    monkeypatch.setattr(ga, "WINDOW_DAYS", 10)
    fail = {"on": "2026-01-11"}

    def respond(endpoint, params):
        if params["date_gte"] == fail["on"]:
            return _Resp(400, {"error": "boom"})
        return _Resp(200, {"count": 1, "data": _rows(1, params["date_gte"])})
    client, session = _client(respond)
    with pytest.raises(ga.AlphanumeError):
        ga.pull_range(conn, client, "qgmi", date(2026, 1, 1), date(2026, 1, 30))
    fail["on"] = None
    session.calls.clear()
    ga.pull_range(conn, client, "qgmi", date(2026, 1, 1), date(2026, 1, 30))
    assert [p["date_gte"] for _, p in session.calls] == ["2026-01-11", "2026-01-21"]


def test_per_ticker_pull_uses_tickers_already_captured(conn, monkeypatch):
    monkeypatch.setattr(ga, "HISTORY_START", date(2026, 1, 1))
    monkeypatch.setattr(ga, "JOBS", {**ga.JOBS, "market_cap": {
        **ga.JOBS["market_cap"], "tickers_from": ["qgmi"]}})
    client, session = _client(lambda e, p: _Resp(200, {
        "count": 2, "data": [{"date": "2026-01-02", "ticker": "AAA"},
                             {"date": "2026-01-02", "ticker": "BBB"}]}))
    ga.pull(conn, client, "qgmi", date(2026, 1, 31))
    session.calls.clear()
    ga.pull(conn, client, "market_cap", date(2026, 1, 31))
    assert sorted(p["ticker"] for _, p in session.calls) == ["AAA", "BBB"]


def test_live_capture_is_kept_apart_from_history(conn):
    client, session = _client(lambda e, p: _Resp(200, {"count": 2, "data": _rows(2)}))
    ga.live(conn, client, "ndm", date(2026, 10, 28))
    ga.live(conn, client, "ndm", date(2026, 10, 28))
    assert session.calls[0][1] == {"date_gte": "2026-10-21", "date_lte": "2026-10-28"}
    (s,) = ga.summary(conn)
    assert (s["capture"], s["pages"], s["rows"]) == ("live", 2, 4)
