from datetime import date

import pandas as pd

from scripts.get_ig_margins import (
    RAW_COLUMNS,
    download_spreadsheet,
    parse_tiered_spreadsheet,
    write_to_sqlite,
)
from src.eodhd_io import Database


def _write_fixture_xlsx(path, rows):
    """Build a spreadsheet matching the real file's shape: 4 blank/header
    rows, then data starting at row index 4 (so header=4 in read_excel
    lands exactly on the real data), with an extra junk column beyond the
    first 9 to mirror the real sheet's further (ignored) tier columns.

    Built from plain positional row-lists (not labeled DataFrames) --
    pd.concat on frames with mismatched column labels reorders by label,
    not position, which silently scrambled an earlier version of this
    fixture into the wrong physical columns.
    """
    header_pad = [[None] * 10] * 4
    header_row = [RAW_COLUMNS + ["junk"]]
    data_rows = [
        [r[c] for c in RAW_COLUMNS] + [r.get("junk_tier2_col", "ignored")]
        for r in rows
    ]
    pd.DataFrame(header_pad + header_row + data_rows).to_excel(path, header=False, index=False)


BASE_ROW = {
    "new_positions": "No", "shortable": "No", "guaranteed_stop_premium": "0.3%",
    "borrow_rate": "1%", "tier1_rate": "25%", "junk_tier2_col": "ignored",
}


def _row(reuters, description, country, tier1_range="0 - 100,000", **overrides):
    r = {**BASE_ROW, "reuters": reuters, "description": description,
         "country": country, "tier1_range": tier1_range}
    r.update(overrides)
    return r


def test_parse_filters_to_au_us_only(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("BHP.AX", "BHP Group", "AUSTRALIA"),
        _row("AAPL.O", "Apple Inc", "UNITED STATES"),
        _row("VOD.L", "Vodafone", "UNITED KINGDOM"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    assert set(df["code"]) == {"BHP.AU", "AAPL.US"}


def test_parse_drops_error_rows(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("BHP.AX", "BHP Group", "AUSTRALIA"),
        _row("BAD.AX", "Bad Row", "AUSTRALIA", guaranteed_stop_premium="ERROR"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    assert list(df["code"]) == ["BHP.AU"]


def test_parse_drops_caret_suffixed_retired_rics(tmp_path):
    """A caret-suffixed Reuters code is a retired RIC -- dropping it is
    what makes (code) collision-free when a ticker gets reused (e.g.
    MTL.AX / PLC.AX / DOC.N in the real data)."""
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("MTL.AX^C21", "Old delisted Manalto", "AUSTRALIA"),
        _row("MTL.AX", "Current Mantle Minerals", "AUSTRALIA"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    assert list(df["code"]) == ["MTL.AU"]
    assert df.iloc[0]["description"] == "Current Mantle Minerals"
    assert not df.duplicated(subset=["code", "download_date"]).any()


def test_parse_nulls_sentinel_tier1_range_instead_of_correcting(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("GOOD.AX", "Good Row", "AUSTRALIA", tier1_range="0 - 4,799,500"),
        _row("BAD.AX", "Sentinel Row", "AUSTRALIA", tier1_range="0 - -571,901,888"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    good = df[df["code"] == "GOOD.AU"].iloc[0]
    bad = df[df["code"] == "BAD.AU"].iloc[0]
    assert good["tier1_limit"] == 4799500
    assert pd.isna(bad["tier1_limit"])
    assert pd.isna(bad["tier1_rate"])


def test_parse_tier1_limit_and_rate_are_numeric(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("BHP.AX", "BHP Group", "AUSTRALIA", tier1_range="0 - 42,900", tier1_rate="20%"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    row = df.iloc[0]
    assert row["tier1_limit"] == 42900
    assert row["tier1_rate"] == 20


def test_parse_sets_download_date(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [_row("BHP.AX", "BHP Group", "AUSTRALIA")])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))

    assert df.iloc[0]["download_date"] == "2026-10-01"


def test_write_to_sqlite_keeps_history_across_months(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [_row("BHP.AX", "BHP Group", "AUSTRALIA")])

    with Database(tmp_path / "test.db") as db:
        write_to_sqlite(parse_tiered_spreadsheet(xlsx, date(2026, 8, 1)), db)
        write_to_sqlite(parse_tiered_spreadsheet(xlsx, date(2026, 9, 1)), db)

        rows = db.conn.execute(
            "SELECT download_date FROM ig_margins WHERE code='BHP.AU' ORDER BY download_date"
        ).fetchall()

    assert [r[0] for r in rows] == ["2026-08-01", "2026-09-01"]


def test_write_to_sqlite_is_idempotent_same_download_date(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [_row("BHP.AX", "BHP Group", "AUSTRALIA", tier1_rate="20%")])

    with Database(tmp_path / "test.db") as db:
        df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 1))
        write_to_sqlite(df, db)
        write_to_sqlite(df, db)  # same (code, download_date) -- must replace, not duplicate

        count = db.conn.execute(
            "SELECT COUNT(*) FROM ig_margins WHERE code='BHP.AU' AND download_date='2026-10-01'"
        ).fetchone()[0]

    assert count == 1


def test_write_to_sqlite_nulls_round_trip_as_sql_null(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("BAD.AX", "Sentinel Row", "AUSTRALIA", tier1_range="0 - -571,901,888"),
    ])

    with Database(tmp_path / "test.db") as db:
        write_to_sqlite(parse_tiered_spreadsheet(xlsx, date(2026, 10, 1)), db)
        row = db.conn.execute(
            "SELECT tier1_limit, tier1_rate FROM ig_margins WHERE code='BAD.AU'"
        ).fetchone()

    assert row == (None, None)


def test_download_spreadsheet_skips_existing_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("scripts.get_ig_margins.run", lambda *a, **k: calls.append(a))
    out_path = tmp_path / "tiered.2026-10-01.xlsx"
    out_path.write_bytes(b"already here")

    download_spreadsheet(out_path)

    assert calls == []
    assert out_path.read_bytes() == b"already here"


def test_download_spreadsheet_curls_when_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("scripts.get_ig_margins.run", lambda *a, **k: calls.append((a, k)))
    out_path = tmp_path / "tiered.2026-10-01.xlsx"

    download_spreadsheet(out_path)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert str(out_path) in args[0]
    assert kwargs.get("check") is True
