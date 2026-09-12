from datetime import date
from unittest import mock

import pandas as pd
import pytest
import requests
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.pandas import column, data_frames, range_indexes

from src.eodhd_io import (
    Database,
    add_local_time,
    csv2pandas_daily,
    csv2pandas_intraday,
    fetch_daily,
    pandas2polars,
    polars2pandas,
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


@settings(max_examples=50)
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
