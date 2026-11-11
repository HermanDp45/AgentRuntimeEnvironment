import pytest
from dates import days_between


def test_leap():
    assert days_between("2024-02-28", "2024-03-01") == 2
    assert days_between("2025-12-31", "2026-01-01") == 1
    assert days_between("2026-01-02", "2026-01-01") == -1


def test_invalid():
    for s in ("", "２０２６-01-01", "2025-02-29"):
        with pytest.raises(ValueError):
            days_between(s, "2026-01-01")
