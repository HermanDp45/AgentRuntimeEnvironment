from calc import average


def test_average():
    assert average([2, 4]) == 3
    assert average([]) == 0
    assert average([-3, 3]) == 0
