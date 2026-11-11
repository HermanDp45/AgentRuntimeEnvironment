from parse_logs import aggregate


def test_hours():
    assert aggregate(
        [
            "2026-01-01T12:10:00 ok",
            "2026-01-01T12:59:00 err",
            "2026-01-02T12:00:00 ok",
            "2026-01-01T13:05:00 ok",
            "",
        ]
    ) == {"2026-01-01T12": 2, "2026-01-02T12": 1, "2026-01-01T13": 1}


def test_empty():
    assert aggregate([]) == {}
