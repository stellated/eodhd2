from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pandas as pd
import pytest
import requests
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra.pandas import column, data_frames, range_indexes

from src.eodhd_io import (
    DAILY_CSV_COLUMNS,
    Database,
    _eodhd_fetch_csv,
    _fetch_daily_resolved,
    _TransientEodhdResponse,
    add_local_time,
    add_ticker_alias,
    csv2pandas_daily,
    csv2pandas_intraday,
    fetch_daily,
    pandas2polars,
    polars2pandas,
    tips,
    unresolved_tips,
)


# --- Fixtures for Temporary CSV Files ---
@pytest.fixture
def sample_daily_csv(tmp_path):
    """Generate a temporary daily CSV file for testing."""
    df = pd.DataFrame({
        "Date": ["2022-01-01", "2022-01-02"],
        "Open": [100.0, 101.0],
        "High": [101.0, 102.0],
        "Low": [99.0, 100.0],
        "Close": [100.5, 101.5],
        "Adjusted_close": [100.3, 101.3],
        "Volume": [1000000, 1200000],
    })
    csv_path = tmp_path / "sample_daily.csv"
    df.to_csv(csv_path, index=False)
    return csv_path

@pytest.fixture
def invalid_daily_csv(tmp_path):
    """Generate a temporary invalid daily CSV file (missing columns)."""
    df = pd.DataFrame({
        "Date": ["2022-01-01"],
        "Open": [100.0],
    })
    csv_path = tmp_path / "invalid_daily.csv"
    df.to_csv(csv_path, index=False)
    return csv_path

@pytest.fixture
def sample_intraday_csv(tmp_path):
    """Generate a temporary intraday CSV file for testing."""
    df = pd.DataFrame({
        "Timestamp": [1609459200, 1609459260],
        "Open": [100.0, 100.5],
        "High": [101.0, 101.5],
        "Low": [99.0, 100.0],
        "Close": [100.5, 101.0],
        "Volume": [1000000, 1200000],
        "Gmtoffset": [0, 0],
        "Datetime": ["2022-01-01 00:00:00", "2022-01-01 00:01:00"],
    })
    csv_path = tmp_path / "sample_intraday.csv"
    df.to_csv(csv_path, index=False)
    return csv_path

@pytest.fixture
def modified_intraday_csv(tmp_path, sample_intraday_csv):
    """Generate a temporary intraday CSV file with non-zero Gmtoffset."""
    df = pd.read_csv(sample_intraday_csv)
    df.loc[0, "Gmtoffset"] = 1
    modified_csv_path = tmp_path / "modified_intraday.csv"
    df.to_csv(modified_csv_path, index=False)
    return modified_csv_path

# --- Tests for csv2pandas_daily ---
@mock.patch("src.eodhd_io._get_calendar")
def test_csv2pandas_daily_valid(mock_get_calendar, sample_daily_csv):
    """Test with a dynamically generated daily CSV."""
    mock_cal = mock.MagicMock()
    mock_cal.first_session.date.return_value = date(2020, 1, 1)

    sessions = pd.date_range("2022-01-01", "2022-01-02", freq="D")
    mock_cal.sessions_in_range.return_value = sessions

    schedule_data = pd.DataFrame({
        "open": [pd.Timestamp("2022-01-01 09:30:00"), pd.Timestamp("2022-01-02 09:30:00")],
        "close": [pd.Timestamp("2022-01-01 16:00:00"), pd.Timestamp("2022-01-02 16:00:00")],
    }, index=sessions)
    mock_cal.schedule = schedule_data

    mock_get_calendar.return_value = mock_cal

    pdf = csv2pandas_daily("AAPL.US", sample_daily_csv)
    assert len(pdf) == 2
    assert "timestamp" in pdf.columns

def test_csv2pandas_daily_missing_columns(invalid_daily_csv):
    """Test with a CSV missing required columns."""
    with pytest.raises(ValueError, match="Missing required columns"):
        csv2pandas_daily("AAPL.US", invalid_daily_csv)

# --- Tests for csv2pandas_intraday ---
def test_csv2pandas_intraday_gmtoffset_warning(modified_intraday_csv, caplog):
    """Test warning for non-zero Gmtoffset."""
    with caplog.at_level(30):
        csv2pandas_intraday("AAPL.US", modified_intraday_csv, "5m")
        assert "Non-zero Gmtoffset detected" in caplog.text

def test_csv2pandas_intraday_has_no_local_time(sample_intraday_csv):
    """local_time is only ever added deliberately, via add_local_time() --
    csv2pandas_intraday() must not include it (it briefly did, by accident;
    see doc/DESIGN_DECISIONS.md, "local_time only ever added deliberately")."""
    pdf = csv2pandas_intraday("AAPL.US", sample_intraday_csv, "5m")
    assert "local_time" not in pdf.columns

# --- Tests for pandas/polars round-trip ---
def test_pandas_polars_roundtrip():
    """Test round-trip: pandas -> polars -> pandas."""
    pdf = pd.DataFrame({
        "code": ["AAPL.US"],
        "timestamp": [1609459200],
        "datetime": pd.to_datetime(["2022-01-01"], utc=True).tz_localize(None),
        "date": [date(2022, 1, 1)],
        "op": [100.0],
        "hi": [101.0],
        "lo": [99.0],
        "cl": [100.5],
        "ac": [100.3],
        "vo": [1000000],
    })
    df = pandas2polars(pdf)
    pdf2 = polars2pandas(df)
    pd.testing.assert_frame_equal(pdf, pdf2)

# --- Tests for add_local_time ---
def test_add_local_time(sample_intraday_csv):
    """Test add_local_time for intraday DataFrame."""
    pdf = csv2pandas_intraday("AAPL.US", sample_intraday_csv, "5m")
    pdf_with_time = add_local_time(pdf)
    assert "local_time" in pdf_with_time.columns

# --- Tests for Database ---
@mock.patch("src.eodhd_io._get_calendar")
def test_database_from_csv(mock_get_calendar, tmp_path, sample_daily_csv):
    """Test Database.from_csv with a dynamically generated CSV."""
    mock_cal = mock.MagicMock()
    mock_cal.first_session.date.return_value = date(2020, 1, 1)

    sessions = pd.date_range("2022-01-01", "2022-01-02", freq="D")
    mock_cal.sessions_in_range.return_value = sessions

    schedule_data = pd.DataFrame({
        "open": [pd.Timestamp("2022-01-01 09:30:00"), pd.Timestamp("2022-01-02 09:30:00")],
        "close": [pd.Timestamp("2022-01-01 16:00:00"), pd.Timestamp("2022-01-02 16:00:00")],
    }, index=sessions)
    mock_cal.schedule = schedule_data

    mock_get_calendar.return_value = mock_cal

    db_path = tmp_path / "test.db"
    with Database(db_path) as db:
        db.from_csv("AAPL.US", sample_daily_csv, "1d", "daily")
        pdf = db.to_pandas("daily", code="AAPL.US", interval="1d")
        assert len(pdf) == 2

def test_database_to_pandas_missing_token(tmp_path):
    """Test error for missing api_token."""
    db_path = tmp_path / "test.db"
    with pytest.raises(ValueError, match="api_token is required"):
        with Database(db_path) as db:
            db._require_token()  # Directly call to raise ValueError

# --- Tests for fetch_daily (mocked) ---
@mock.patch("src.eodhd_io._fetch_with_retry")
@mock.patch("src.eodhd_io._get_calendar")
def test_fetch_daily(mock_get_calendar, mock_fetch):
    """Test fetch_daily with a mocked response."""
    mock_response = mock.MagicMock()
    mock_response.text = (
        "Date,Open,High,Low,Close,Adjusted_close,Volume\n"
        "2022-01-01,100,101,99,100.5,100.3,1000000"
    )
    mock_response.raise_for_status = lambda: None
    mock_fetch.return_value = mock_response

    mock_cal = mock.MagicMock()
    mock_cal.first_session.date.return_value = date(2020, 1, 1)
    sessions = pd.date_range("2022-01-01", "2022-01-01", freq="D")
    schedule_data = pd.DataFrame({
        "open": [pd.Timestamp("2022-01-01 09:30:00")],
        "close": [pd.Timestamp("2022-01-01 16:00:00")],
    }, index=sessions)
    mock_cal.schedule = schedule_data
    mock_cal.sessions_in_range.return_value = sessions
    mock_get_calendar.return_value = mock_cal

    pdf = fetch_daily("AAPL.US", "fake_token")
    assert len(pdf) == 1

@mock.patch("src.eodhd_io._fetch_with_retry")
@mock.patch("src.eodhd_io._get_calendar")
def test_fetch_daily_empty(mock_get_calendar, mock_fetch):
    """Test fetch_daily with an empty response.
    csv2pandas_daily raises ValueError when no rows remain (see its
    "raises if all clipped" behaviour) -- an empty CSV has nothing to
    clip either, so it hits the same raise.
    """
    mock_response = mock.MagicMock()
    mock_response.text = "Date,Open,High,Low,Close,Adjusted_close,Volume\n"
    mock_response.raise_for_status = lambda: None
    mock_fetch.return_value = mock_response

    mock_cal = mock.MagicMock()
    mock_cal.first_session.date.return_value = date(2020, 1, 1)
    mock_get_calendar.return_value = mock_cal

    with pytest.raises(ValueError, match="no rows remain"):
        fetch_daily("AAPL.US", "fake_token")

@mock.patch("src.eodhd_io._fetch_with_retry")
def test_fetch_daily_error(mock_fetch):
    """Test fetch_daily with an HTTP error."""
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    mock_fetch.return_value = mock_response

    with pytest.raises(requests.HTTPError):
        fetch_daily("AAPL.US", "fake_token")


# --- Tests for the transient-bad-response retry (200 OK, non-CSV body) ---
@mock.patch("time.sleep")
@mock.patch("src.eodhd_io._fetch_with_retry")
def test_eodhd_fetch_csv_retries_transient_bad_response(mock_fetch, mock_sleep):
    """A 200 OK with a non-CSV body (e.g. a rate-limit message returned
    without a 4xx/5xx status) is retried once; a valid response on the
    retry succeeds transparently."""
    bad_response = mock.MagicMock(status_code=200, text='{"error": "rate limited"}')
    bad_response.raise_for_status = lambda: None
    good_response = mock.MagicMock(
        status_code=200,
        text="Date,Open,High,Low,Close,Adjusted_close,Volume\n"
             "2022-01-01,100,101,99,100.5,100.3,1000000",
    )
    good_response.raise_for_status = lambda: None
    mock_fetch.side_effect = [bad_response, good_response]

    raw = _eodhd_fetch_csv("https://example.com/eod/AAPL.US?api_token=secret", DAILY_CSV_COLUMNS)

    assert set(DAILY_CSV_COLUMNS).issubset(raw.columns)
    assert mock_fetch.call_count == 2
    mock_sleep.assert_called_once()

@mock.patch("time.sleep")
@mock.patch("src.eodhd_io._fetch_with_retry")
def test_eodhd_fetch_csv_gives_up_after_retry(mock_fetch, mock_sleep):
    """A persistently bad response (never valid CSV) fails after the retry
    budget is exhausted, with the token redacted and the status/body visible
    in the error for diagnosis."""
    bad_response = mock.MagicMock(status_code=200, text='{"error": "rate limited"}')
    bad_response.raise_for_status = lambda: None
    mock_fetch.return_value = bad_response

    with pytest.raises(_TransientEodhdResponse) as exc_info:
        _eodhd_fetch_csv(
            "https://example.com/eod/AAPL.US?api_token=secret123", DAILY_CSV_COLUMNS
        )

    assert mock_fetch.call_count == 2
    assert "secret123" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)
    assert "rate limited" in str(exc_info.value)


# --- Tests for tips()'s per-tip resilience (one bad tip doesn't abort the run) ---
@mock.patch("src.eodhd_io.fetch_daily")
@mock.patch("src.eodhd_io._get_calendar")
def test_tips_skips_failing_tip_and_continues(
    mock_get_calendar, mock_fetch_daily, tmp_path, caplog
):
    """If fetch_daily raises for one tip, tips() logs it and still fetches
    and stores every other tip in the list -- it must not abort the whole
    batch over a single bad ticker/response."""
    mock_get_calendar.return_value = mock.MagicMock()  # unused: n1=0 short-circuits it
    tip_date = date(2026, 9, 1)

    def fake_fetch_daily(code, api_token, from_date=None, to_date=None):
        if code == "BAD.US":
            raise ValueError("simulated EODHD failure")
        ts = int(pd.Timestamp(tip_date).timestamp())
        return pd.DataFrame({
            "code": [code],
            "timestamp": [ts],
            "datetime": [pd.Timestamp(tip_date)],
            "date": [tip_date],
            "op": [100.0], "hi": [101.0], "lo": [99.0], "cl": [100.5], "ac": [100.5],
            "vo": [1000],
        })

    mock_fetch_daily.side_effect = fake_fetch_daily

    with Database(tmp_path / "test.db", api_token="fake_token") as db:
        with caplog.at_level("ERROR"):
            tips(
                db,
                [("BAD.US", tip_date), ("GOOD.US", tip_date)],
                "daily",
                "1d",
                n1=0,
                n2=0,
            )
        pdf = pd.read_sql("SELECT code FROM daily", db.conn)

    assert list(pdf["code"]) == ["GOOD.US"]
    assert any("BAD.US" in rec.message for rec in caplog.records)


@mock.patch("src.eodhd_io.fetch_intraday")
@mock.patch("src.eodhd_io._get_calendar")
def test_tips_intraday_writes_local_time(mock_get_calendar, mock_fetch_intraday, tmp_path):
    """fetch_intraday()/csv2pandas_intraday() deliberately don't include
    local_time (see doc/DESIGN_DECISIONS.md, "local_time only ever added
    deliberately") -- tips() must add it itself before writing, since this
    is the one place intraday data actually lands in the db."""
    mock_get_calendar.return_value = mock.MagicMock()  # unused: n1=0 short-circuits it
    tip_date = date(2026, 9, 1)
    # Explicit UTC instant (not pd.Timestamp(date).timestamp(), which is
    # ambiguous between naive-local and UTC) -- 13:30 UTC = 09:30 EDT, so
    # the expected local_time below is exact, not system-timezone-dependent.
    ts = int(datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc).timestamp())

    def fake_fetch_intraday(code, api_token, interval, from_ts=None, to_ts=None):
        return pd.DataFrame({
            "code": [code],
            "timestamp": [ts],
            "datetime": [pd.Timestamp(tip_date)],
            "local_date": [tip_date],
            "op": [100.0], "hi": [101.0], "lo": [99.0], "cl": [100.5],
            "vo": [1000],
        })

    mock_fetch_intraday.side_effect = fake_fetch_intraday

    with Database(tmp_path / "test.db", api_token="fake_token") as db:
        tips(db, [("GOOD.US", tip_date)], "intraday_5m", "5m", n1=0, n2=0)
        pdf = pd.read_sql("SELECT code, local_time FROM intraday_5m", db.conn)

    assert list(pdf["local_time"]) == ["09:30:00"]


# --- Tests for unresolved_tips() ---
def _seed_daily_dates(db, rows):
    """rows: list of (code, date_str). Minimal schema -- only the columns
    unresolved_tips()'s query touches."""
    db.conn.execute("CREATE TABLE daily (code TEXT, date TEXT)")
    db.conn.executemany("INSERT INTO daily (code, date) VALUES (?, ?)", rows)
    db.conn.commit()

def test_unresolved_tips_flags_closed_short_windows_only(tmp_path):
    """A tip is only "unresolved" if its backfill window has fully closed
    AND coverage is still short of n2 days -- not if the window is still
    open (too recent to expect full coverage yet) and not if it's already
    fully covered."""
    n2 = 5
    today = date.today()
    closed_tip_date = today - timedelta(days=(n2 * 2 + 5) + 10)  # window long closed
    open_tip_date = today - timedelta(days=2)  # window still open

    with Database(tmp_path / "test.db") as db:
        rows = []
        # FULL.US: closed window, fully covered (n2 + 1 days from tip_date)
        rows += [
            ("FULL.US", (closed_tip_date + timedelta(days=i)).isoformat())
            for i in range(n2 + 1)
        ]
        # PARTIAL.US: closed window, only 2 days of coverage (LBRDA-style)
        rows += [
            ("PARTIAL.US", (closed_tip_date + timedelta(days=i)).isoformat())
            for i in range(2)
        ]
        # ZERO.US: closed window, no rows at all (GMGI-style) -- not seeded
        # STILLOPEN.US: window still open, minimal coverage -- must NOT be flagged
        rows.append(("STILLOPEN.US", open_tip_date.isoformat()))
        _seed_daily_dates(db, rows)

        tip_list = [
            ("FULL.US", closed_tip_date),
            ("PARTIAL.US", closed_tip_date),
            ("ZERO.US", closed_tip_date),
            ("STILLOPEN.US", open_tip_date),
        ]
        result = unresolved_tips(db, tip_list, "daily", "1d", n2=n2)

    assert set(result["code"]) == {"PARTIAL.US", "ZERO.US"}

    partial = result[result["code"] == "PARTIAL.US"].iloc[0]
    assert partial["actual_days"] == 2
    assert partial["last_available_date"] == closed_tip_date + timedelta(days=1)

    zero = result[result["code"] == "ZERO.US"].iloc[0]
    assert zero["actual_days"] == 0
    assert zero["last_available_date"] is None


# --- Tests for ticker_aliases / add_ticker_alias() / _fetch_daily_resolved() ---
def _daily_row(code, d, price, vo=100):
    return pd.DataFrame({
        "code": [code], "timestamp": [0], "datetime": [pd.Timestamp(d)],
        "date": [d], "op": [price], "hi": [price], "lo": [price], "cl": [price],
        "ac": [price], "vo": [vo],
    })

def test_add_ticker_alias_requires_exactly_one_target(tmp_path):
    with Database(tmp_path / "test.db") as db:
        with pytest.raises(ValueError, match="Exactly one"):
            add_ticker_alias(db, "OLD.US", date(2026, 1, 1))  # neither given
        with pytest.raises(ValueError, match="Exactly one"):
            add_ticker_alias(
                db, "OLD.US", date(2026, 1, 1), new_code="NEW.US", cash_price=5.0
            )  # both given

@mock.patch("src.eodhd_io.fetch_daily")
def test_fetch_daily_resolved_no_alias_is_a_passthrough(mock_fetch_daily, tmp_path):
    mock_fetch_daily.return_value = _daily_row("X.US", date(2026, 1, 1), 10.0)
    with Database(tmp_path / "test.db") as db:
        result = _fetch_daily_resolved(db, "X.US", "tok", date(2026, 1, 1), date(2026, 1, 5))

    mock_fetch_daily.assert_called_once_with(
        "X.US", "tok", from_date=date(2026, 1, 1), to_date=date(2026, 1, 5)
    )
    assert list(result["code"]) == ["X.US"]

@mock.patch("src.eodhd_io.fetch_daily")
def test_fetch_daily_resolved_splices_renamed_ticker_with_ratio(mock_fetch_daily, tmp_path):
    """A rename/split (OLD.US -> NEW.US, ratio 0.2) should return one
    continuous series labelled OLD.US throughout, with post-alias prices
    scaled by ratio and volume left untouched (not meaningfully comparable
    across the split)."""
    def fake_fetch(code, api_token, from_date=None, to_date=None):
        if code == "OLD.US":
            return _daily_row("OLD.US", date(2026, 1, 5), 10.0, vo=100)
        if code == "NEW.US":
            return _daily_row("NEW.US", date(2026, 1, 10), 50.0, vo=200)
        raise AssertionError(f"unexpected code {code}")
    mock_fetch_daily.side_effect = fake_fetch

    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(
            db, "OLD.US", date(2026, 1, 8), new_code="NEW.US", ratio=0.2, reason="test rename"
        )
        result = _fetch_daily_resolved(db, "OLD.US", "tok", date(2026, 1, 1), date(2026, 1, 15))

    calls = mock_fetch_daily.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == "OLD.US"
    assert calls[0].kwargs == {"from_date": date(2026, 1, 1), "to_date": date(2026, 1, 7)}
    assert calls[1].args[0] == "NEW.US"
    assert calls[1].kwargs == {"from_date": date(2026, 1, 8), "to_date": date(2026, 1, 15)}

    assert list(result["code"]) == ["OLD.US", "OLD.US"]
    assert list(result["date"]) == [date(2026, 1, 5), date(2026, 1, 10)]
    assert result.iloc[0]["cl"] == 10.0  # pre-alias: untouched
    assert result.iloc[1]["cl"] == pytest.approx(10.0)  # post-alias: 0.2 * 50.0
    assert result.iloc[1]["vo"] == 200  # volume NOT rescaled

@mock.patch("src.eodhd_io.fetch_daily")
@mock.patch("src.eodhd_io._get_calendar")
def test_fetch_daily_resolved_cash_out(mock_get_calendar, mock_fetch_daily, tmp_path):
    """A pure cash-for-scrip takeover (no new_code) should pad flat,
    zero-volume rows at cash_price for every real session from
    effective_date onward -- same convention as ordinary missing-session
    padding."""
    mock_fetch_daily.return_value = _daily_row("OLD.US", date(2026, 1, 5), 10.0)

    sessions = pd.DatetimeIndex([pd.Timestamp("2026-01-08"), pd.Timestamp("2026-01-09")])
    schedule = pd.DataFrame(
        {"open": [pd.Timestamp("2026-01-08 09:30:00"), pd.Timestamp("2026-01-09 09:30:00")]},
        index=sessions,
    )
    mock_cal = mock.MagicMock()
    mock_cal.sessions_in_range.return_value = sessions
    mock_cal.schedule = schedule
    mock_get_calendar.return_value = mock_cal

    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "OLD.US", date(2026, 1, 8), cash_price=42.0, reason="test cash-out")
        result = _fetch_daily_resolved(db, "OLD.US", "tok", date(2026, 1, 1), date(2026, 1, 9))

    assert list(result["date"]) == [date(2026, 1, 5), date(2026, 1, 8), date(2026, 1, 9)]
    assert result.iloc[0]["cl"] == 10.0
    for i in (1, 2):
        assert result.iloc[i]["cl"] == 42.0
        assert result.iloc[i]["vo"] == 0

# --- Hypothesis Test ---
# CHANGED: the @given(...) strategy below used to be attached to a
# vestigial test_min_date(df) (no assertions, just a print) that sat
# between this comment and test_pandas_polars_roundtrip_hypothesis --
# leaving the real property test below with only @settings and no
# @given, so pytest treated `df` as a missing fixture and errored at
# collection. test_min_date has been removed (it wasn't testing
# anything) and @given now decorates the test it was clearly meant for.
# Also added dtype= to every column: newer hypothesis requires an
# explicit dtype on every column when data_frames() is combined with an
# explicit row-count strategy. And `rows=st.integers(...)` was always
# wrong -- `rows` in data_frames() expects a strategy producing whole row
# tuples, not a row count; row count is controlled via `index=`.
_nonneg_int = st.integers(min_value=0, max_value=2**31 - 1)
_nonneg_float = st.floats(min_value=0, allow_nan=False, allow_infinity=False)


# too_slow suppressed and deadline disabled: fails on constrained hardware
# (e.g. a low-CPU VM under concurrent load) where drawing/running a single
# example can legitimately take >200ms -- not a sign of a bad strategy or a
# real performance regression (observed: passes reliably in isolation,
# only flakes when the suite runs concurrently with other load on a
# 1-core VM).
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
@given(
    df=data_frames(
        columns=[
            column("code", dtype=str, elements=st.text(min_size=1, max_size=10)),
            column("timestamp", dtype="int64", elements=_nonneg_int),
            column("datetime", dtype=object, elements=st.datetimes()),
            column("date", dtype=object, elements=st.dates()),
            column("op", dtype="float64", elements=_nonneg_float),
            column("hi", dtype="float64", elements=_nonneg_float),
            column("lo", dtype="float64", elements=_nonneg_float),
            column("cl", dtype="float64", elements=_nonneg_float),
            column("ac", dtype="float64", elements=_nonneg_float),
            column("vo", dtype="int64", elements=_nonneg_int),
        ],
        index=range_indexes(min_size=1, max_size=100),
    )
)
def test_pandas_polars_roundtrip_hypothesis(df):
    """Test round-trip conversion with hypothesis."""
    df["code"] = df["code"].astype(str)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["datetime"] = pd.to_datetime(df["datetime"]).astype("datetime64[us]")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["op"] = df["op"].astype("float64")
    df["hi"] = df["hi"].astype("float64")
    df["lo"] = df["lo"].astype("float64")
    df["cl"] = df["cl"].astype("float64")
    df["ac"] = df["ac"].astype("float64")
    df["vo"] = df["vo"].astype("int64")

    df_polars = pandas2polars(df)
    df2 = polars2pandas(df_polars)
    pd.testing.assert_frame_equal(df, df2)  # Fixed: Use assert_frame_equal
