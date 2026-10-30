import pytest

from scripts.build_sda_extract import (
    available_date,
    reached_aliases,
    ticker_codes,
    trim_aliases_doc,
    window_end,
)

DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"]


@pytest.mark.parametrize("tip_date, n2, expected", [
    ("2026-09-01", 2, "2026-09-03"),   # tip day is day 0
    ("2026-09-05", 1, "2026-09-08"),   # tip on a non-session: next date is day 0
    ("2026-09-02", 10, "2026-09-08"),  # fewer than n2: the last date present
    ("2026-09-09", 2, "2026-09-09"),   # nothing after: the tip date itself
])
def test_window_end(tip_date, n2, expected):
    assert window_end(DATES, tip_date, n2) == expected


@pytest.mark.parametrize("d, lag, expected", [
    ("2026-09-01", 1, "2026-09-02"),
    ("2026-09-01", 2, "2026-09-03"),
    ("2026-09-05", 1, "2026-09-08"),   # a Saturday row: the next session
    ("2026-09-04", 2, None),           # calendar runs out
])
def test_available_date(d, lag, expected):
    assert available_date(d, DATES, lag) == expected


def test_ticker_codes_maps_class_shares():
    assert ticker_codes({"BRK-B.US", "AAPL.US"}) == {"BRK.B": "BRK-B.US", "AAPL": "AAPL.US"}


def test_trim_aliases_doc_drops_hold_out_tips():
    doc = (
        "Intro, 2026-10-30.\n\n"
        "| old_code | new_code | effective_date | Tip dates affected | Notes |\n"
        "|---|---|---|---|---|\n"
        "| A.US | B.US | 2026-01-01 | 2026-08-27, 2026-09-21 | both |\n"
        "| C.US | D.US | 2026-02-01 | 2026-09-25 | hold-out only, outcome X |\n"
        "\n"
        "| code | tip_date | Why |\n"
        "|---|---|---|\n"
        "| E.US | 2026-08-13 | kept |\n"
    )
    trimmed = trim_aliases_doc(doc, "2026-09-01")
    assert "| A.US | B.US | 2026-01-01 | 2026-08-27 | both |" in trimmed
    assert "C.US" not in trimmed
    assert "| E.US | 2026-08-13 | kept |" in trimmed
    assert trimmed.startswith("Intro, 2026-10-30.")


def test_reached_aliases_follows_chains_and_skips_others():
    aliases = [("A.US", "B.US", "2026-01-01"), ("B.US", "C.US", "2026-03-01"),
               ("X.US", "Y.US", "2026-02-01"), ("D.US", None, "2026-04-01")]
    assert set(reached_aliases(aliases, {"A.US", "D.US"})) == {aliases[0], aliases[1], aliases[3]}
