from datetime import date

from email_downloader import _build_search_criteria


def test_build_search_criteria_plain():
    assert _build_search_criteria("x@y.com") == '(FROM "x@y.com")'


def test_build_search_criteria_unseen_only():
    assert _build_search_criteria("x@y.com", unseen_only=True) == '(FROM "x@y.com" UNSEEN)'


def test_build_search_criteria_date_range():
    assert _build_search_criteria(
        "x@y.com", since_date=date(2026, 4, 8), before_date=date(2026, 4, 22)
    ) == '(FROM "x@y.com" SINCE "08-Apr-2026" BEFORE "22-Apr-2026")'


def test_build_search_criteria_unseen_and_since_combine():
    assert _build_search_criteria(
        "x@y.com", unseen_only=True, since_date=date(2026, 4, 8)
    ) == '(FROM "x@y.com" UNSEEN SINCE "08-Apr-2026")'
