import pytest
import requests
from pathlib import Path
import pandas as pd
from datetime import date, datetime
from unittest import mock
from hypothesis import given, strategies as st, settings
from hypothesis.extra.pandas import data_frames

# Import functions/classes from eodhd_io.py
from src.eodhd_io import (
    csv2pandas_daily,
    csv2pandas_intraday,
    pandas2polars,
    polars2pandas,
    add_local_time,
    Database,
    fetch_daily,
    fetch_intraday,
)

# --- Tests for csv2pandas_daily ---
def test_csv2pandas_daily_valid():
    # Test with a valid daily CSV
    pdf = csv2pandas_daily("AAPL.US", Path("tests/data/csv/sample_daily.csv"))
    assert len(pdf) > 0
    assert "timestamp" in pdf.columns

def test_csv2pandas_daily_missing_columns():
    # Test with a CSV missing required columns
    with pytest.raises(ValueError, match="Missing required columns"):
        csv2pandas_daily("AAPL.US", Path("tests/data/csv/invalid_daily.csv"))

# --- Tests for csv2pandas_intraday ---
def test_csv2pandas_intraday_gmtoffset_warning():
    # Test warning for non-zero Gmtoffset
    with pytest.warns(UserWarning, match="Non-zero Gmtoffset"):
        csv2pandas_intraday("AAPL.US", Path("tests/data/csv/sample_intraday.csv"), "5m")

# --- Tests for pandas/polars round-trip ---
def test_pandas_polars_roundtrip():
    # Test round-trip: pandas -> polars -> pandas
    pdf = pd.DataFrame({
        "code": ["AAPL.US"],
        "timestamp": [1609459200],
        "datetime": pd.to_datetime(["2021-01-01"], utc=True).tz_localize(None),
        "date": [date(2021, 1, 1)],
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
def test_add_local_time():
    # Test add_local_time for intraday DataFrame
    pdf = csv2pandas_intraday("AAPL.US", Path("tests/data/csv/sample_intraday.csv"), "5m")
    pdf_with_time = add_local_time(pdf)
    assert "local_time" in pdf_with_time.columns

# --- Tests for Database ---
def test_database_from_csv(tmp_path):
    # Test Database.from_csv
    db_path = tmp_path / "test.db"
    with Database(db_path) as db:
        db.from_csv("AAPL.US", Path("tests/data/csv/sample_daily.csv"), "1d", "daily")
        pdf = db.to_pandas("daily", code="AAPL.US", interval="1d")
        assert len(pdf) > 0

def test_database_to_pandas_missing_token():
    # Test error for missing api_token
    db_path = "test.db"
    with pytest.raises(ValueError, match="api_token is required"):
        with Database(db_path) as db:
            db.to_pandas("daily", code="AAPL.US", interval="1d")

# --- Tests for fetch_daily ---
@mock.patch("requests.get")
def test_fetch_daily(mock_get, tmp_path):
    # Mock API response
    mock_get.return_value.text = "Date,Open,High,Low,Close,Adjusted_close,Volume\n2021-01-01,100,101,99,100.5,100.3,1000000"
    mock_get.return_value.raise_for_status = lambda: None
    pdf = fetch_daily("AAPL.US", "fake_token")
    assert len(pdf) == 1

@mock.patch("requests.get")
def test_fetch_daily_empty(mock_get):
    # Test empty response
    mock_get.return_value.text = "Date,Open,High,Low,Close,Adjusted_close,Volume\n"
    mock_get.return_value.raise_for_status = lambda: None
    pdf = fetch_daily("AAPL.US", "fake_token")
    assert len(pdf) == 0

@mock.patch("requests.get")
def test_fetch_daily_error(mock_get):
    # Test error response
    mock_get.return_value.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    with pytest.raises(requests.HTTPError):
        fetch_daily("AAPL.US", "fake_token")

# --- Hypothesis Test ---
@given(
    df=data_frames({
        "code": st.text(min_size=1, max_size=10),
        "timestamp": st.integers(min_value=0),
        "datetime": st.datetimes(),
        "date": st.dates(),
        "op": st.floats(min_value=0),
        "hi": st.floats(min_value=0),
        "lo": st.floats(min_value=0),
        "cl": st.floats(min_value=0),
        "ac": st.floats(min_value=0),
        "vo": st.integers(min_value=0),
    }, rows=st.integers(min_value=1, max_value=100))
)
@settings(max_examples=50)
def test_pandas_polars_roundtrip_hypothesis(df):
    # Ensure all columns are present and types are compatible
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
    pd.testing.assert_frame_equal(df, df2)