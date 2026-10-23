import os
import zipfile
from datetime import date

import pandas as pd

from scripts.get_ig_margins import (
    RAW_COLUMNS,
    already_loaded,
    create_resolved_view,
    download_spreadsheet,
    parse_tiered_spreadsheet,
    source_modified,
    write_to_sqlite,
)
from src.eodhd_io import Database, add_ticker_alias


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
    assert str(out_path) in args[0] and "-fsSLR" in args[0]
    assert kwargs.get("check") is True


def _resolved(db, code):
    return db.conn.execute(
        "SELECT ig_code, renamed_on, download_date, description FROM ig_margins_resolved "
        "WHERE code = ? ORDER BY download_date", (code,)).fetchall()


def _load_stale_snapshot(tmp_path, db, rows):
    """A file IG last changed on 2024-04-10, downloaded on 2026-10-01."""
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, rows)
    write_to_sqlite(parse_tiered_spreadsheet(xlsx, date(2026, 10, 1), date(2024, 4, 10)), db)


def test_source_modified_is_stored(tmp_path):
    with Database(tmp_path / "test.db") as db:
        _load_stale_snapshot(tmp_path, db, [_row("BHP.AX", "BHP Group", "AUSTRALIA")])
        assert db.conn.execute("SELECT source_modified FROM ig_margins").fetchone() == (
            "2024-04-10",)


def test_resolved_view_finds_new_symbol_under_old_ig_row(tmp_path):
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "GLTO.US", date(2026, 3, 10), new_code="DMRA.US")
        _load_stale_snapshot(tmp_path, db, [_row("GLTO.O", "Galecto Inc", "UNITED STATES")])
        assert _resolved(db, "GLTO.US") == [("GLTO.US", None, "2026-10-01", "Galecto Inc")]
        assert _resolved(db, "DMRA.US") == [("GLTO.US", "2026-03-10", "2026-10-01",
                                             "Galecto Inc")]


def test_resolved_view_follows_chained_renames_nearest_first(tmp_path):
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "AAA.US", date(2025, 1, 1), new_code="BBB.US")
        add_ticker_alias(db, "BBB.US", date(2026, 1, 1), new_code="CCC.US")
        _load_stale_snapshot(tmp_path, db, [_row("AAA.O", "A Corp", "UNITED STATES")])
        assert [r[0] for r in _resolved(db, "CCC.US")] == ["AAA.US"]


def test_resolved_view_prefers_ig_listing_of_new_symbol(tmp_path):
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "CUK.US", date(2026, 5, 7), new_code="CCL.US")
        _load_stale_snapshot(tmp_path, db, [_row("CUK.N", "Carnival plc", "UNITED STATES"),
                                            _row("CCL.N", "Carnival Corp", "UNITED STATES")])
        assert [r[0] for r in _resolved(db, "CCL.US")] == ["CCL.US"]


def test_resolved_view_ignores_rename_before_ig_file(tmp_path):
    """IG's file postdates the rename, so its row under the old symbol is
    some other (or a reused) listing, not the renamed stock."""
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "OLD.US", date(2023, 1, 1), new_code="NEW.US")
        _load_stale_snapshot(tmp_path, db, [_row("OLD.O", "Old Corp", "UNITED STATES")])
        assert _resolved(db, "NEW.US") == []


def test_create_resolved_view_migrates_old_table(tmp_path):
    with Database(tmp_path / "test.db") as db:
        db.conn.execute("CREATE TABLE ig_margins (download_date TEXT, code TEXT, country TEXT, "
                        "reuters TEXT, description TEXT, new_positions TEXT, shortable TEXT, "
                        "guaranteed_stop_premium TEXT, borrow_rate TEXT, tier1_limit INTEGER, "
                        "tier1_rate INTEGER, PRIMARY KEY (code, download_date))")
        add_ticker_alias(db, "X.US", date(2026, 1, 1), new_code="Y.US")
        create_resolved_view(db.conn)
        assert db.conn.execute("SELECT COUNT(*) FROM ig_margins_resolved").fetchone() == (0,)


def test_parse_keeps_fractional_tier1_rate(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [_row("BHP.AX", "BHP Group", "AUSTRALIA", tier1_rate="30.8%")])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 23))

    assert df.iloc[0]["tier1_rate"] == 30.8


def _load_current_snapshot(tmp_path, db, rows):
    """A file IG changed on 2026-10-23, downloaded the same day."""
    xlsx = tmp_path / "tiered_current.xlsx"
    _write_fixture_xlsx(xlsx, rows)
    write_to_sqlite(parse_tiered_spreadsheet(xlsx, date(2026, 10, 23), date(2026, 10, 23)), db)


def test_resolved_view_maps_old_symbol_forward_and_drops_reused_listing(tmp_path):
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "SATS.US", date(2026, 6, 24), new_code="ECHO.US")
        _load_stale_snapshot(tmp_path, db, [_row("SATS.O", "EchoStar Corp", "UNITED STATES")])
        _load_current_snapshot(tmp_path, db, [_row("SATS.O", "Strive Inc", "UNITED STATES"),
                                              _row("ECHO.O", "EchoStar Corp", "UNITED STATES")])
        assert _resolved(db, "SATS.US") == [
            ("SATS.US", None, "2026-10-01", "EchoStar Corp"),
            ("ECHO.US", "2026-06-24", "2026-10-23", "EchoStar Corp")]
        assert _resolved(db, "ECHO.US") == [
            ("SATS.US", "2026-06-24", "2026-10-01", "EchoStar Corp"),
            ("ECHO.US", None, "2026-10-23", "EchoStar Corp")]


def test_resolved_view_drops_cashed_out_symbol_after_takeover(tmp_path):
    with Database(tmp_path / "test.db") as db:
        add_ticker_alias(db, "APLS.US", date(2026, 5, 14), cash_price=41.03)
        _load_stale_snapshot(tmp_path, db, [_row("APLS.O", "Apellis", "UNITED STATES")])
        _load_current_snapshot(tmp_path, db, [_row("APLS.O", "Someone else", "UNITED STATES")])
        assert [r[2] for r in _resolved(db, "APLS.US")] == ["2026-10-01"]


def test_parse_keeps_plain_ric_when_two_share_a_code(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    _write_fixture_xlsx(xlsx, [
        _row("WBCPJ.AX1", "Westpac Capital Notes 7", "AUSTRALIA"),
        _row("WBCPJ.AX", "Westpac Banking Corp - Pfd", "AUSTRALIA"),
    ])

    df = parse_tiered_spreadsheet(xlsx, date(2026, 10, 23))

    assert df[["code", "reuters"]].values.tolist() == [["WBCPJ.AU", "WBCPJ.AX"]]


def test_source_modified_reads_workbook_created_time(tmp_path):
    xlsx = tmp_path / "tiered.xlsx"
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("docProps/core.xml",
                   '<cp:coreProperties xmlns:dcterms="http://purl.org/dc/terms/">'
                   '<dcterms:created xsi:type="dcterms:W3CDTF">2026-10-22T23:02:13Z'
                   '</dcterms:created></cp:coreProperties>')
    assert source_modified(xlsx) == date(2026, 10, 22)


def test_source_modified_falls_back_to_mtime(tmp_path):
    f = tmp_path / "not_a_zip.xlsx"
    f.write_bytes(b"<html>error</html>")
    os.utime(f, (1712790000, 1712790000))  # 2024-04-10 23:00 UTC
    assert source_modified(f) == date(2024, 4, 10)


def test_already_loaded_finds_same_file_version(tmp_path):
    with Database(tmp_path / "test.db") as db:
        assert already_loaded(db, date(2024, 4, 10)) is None
        _load_stale_snapshot(tmp_path, db, [_row("BHP.AX", "BHP Group", "AUSTRALIA")])
        assert already_loaded(db, date(2024, 4, 10)) == "2026-10-01"
        assert already_loaded(db, date(2026, 10, 22)) is None
