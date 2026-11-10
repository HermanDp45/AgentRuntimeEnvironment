from calendar import parse_date


def month_label(raw: str) -> str:
    return parse_date(raw).strftime("%Y-%m")
