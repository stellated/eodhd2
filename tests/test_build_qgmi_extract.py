import pytest

from scripts.build_qgmi_extract import SECTOR_ETF, classify_bar, price_ratio

OK = ("ok", "2019-03-01", "2021-06-30")


def test_classify_bar_usable_bar():
    assert classify_bar("2020-01-02", (1000, 10.0), OK) is None


@pytest.mark.parametrize("bar, status, reason", [
    (None, None, "no_bars"),
    (None, ("error", None, None), "no_bars"),
    (None, ("ok", None, None), "no_bars"),       # fetched, but never a bar with volume
    (None, ("ok", "2021-03-01", "2026-10-28"), "before_first_bar"),  # e.g. a reused ticker
    (None, ("ok", "2017-01-03", "2019-12-31"), "after_last_bar"),
    (None, OK, "missing_day"),
    ((0, 10.0), OK, "zero_volume"),
])
def test_classify_bar_gaps(bar, status, reason):
    assert classify_bar("2020-01-02", bar, status) == reason


def test_price_ratio():
    assert price_ratio(10.0, 1e9, 1e8) == pytest.approx(1.0)
    assert price_ratio(20.0, 1e9, 1e8) == pytest.approx(2.0)
    assert price_ratio(10.0, None, 1e8) is None
    assert price_ratio(10.0, 1e9, 0) is None


def test_sector_etf_map_covers_the_eleven_sector_etfs():
    assert sorted(SECTOR_ETF.values()) == sorted(
        ["XLK", "XLF", "XLV", "XLE", "XLU", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLC"])
