from datetime import datetime
def parse_date(s):
    value = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if value.tzinfo is None: raise ValueError("timezone required")
    return value
