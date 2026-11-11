from slug import slugify


def test_slugify_edges():
    for text, expected in [
        ("Hello World", "hello-world"),
        (" A__B!! ", "a-b"),
        ("", ""),
        ("Привет", ""),
        ("x123", "x123"),
        ("a---b", "a-b"),
        ("a\nb", "a-b"),
    ]:
        assert slugify(text) == expected
