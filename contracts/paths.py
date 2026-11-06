PATH_PATTERN = (
    r"^(?!.*(?:^|/)(?:\.{1,2}|\.git)(?:/|$))"
    r"(?!.*(?:^|/)\._are_)[^/\\\x00]+(?:/[^/\\\x00]+)*$"
)


def safe_path(value: str) -> str:
    parts = value.split("/")
    if (
        "\\" in value
        or "\x00" in value
        or any(part in ("", ".", "..", ".git") or part.startswith("._are_") for part in parts)
    ):
        raise ValueError("Expected a relative /work path without reserved components")
    return value
