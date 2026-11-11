import app
from total import compute_total


def test_renamed():
    assert compute_total([3, 4]) == 7
    assert compute_total([]) == 0
    assert app.value == 3
