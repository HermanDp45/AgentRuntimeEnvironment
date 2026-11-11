import pytest
from bounds import clamp


def test_bounds():
    assert clamp(-3, 0, 5) == 0
    assert clamp(8, 0, 5) == 5
    assert clamp(2, 0, 5) == 2
    assert clamp(1, 2, 2) == 2


def test_invalid():
    with pytest.raises(ValueError):
        clamp(1, 5, 0)
