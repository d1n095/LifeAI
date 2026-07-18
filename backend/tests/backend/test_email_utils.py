from app.email_utils import normalize_email


def test_lowercases():
    assert normalize_email("User@Example.com") == "user@example.com"


def test_strips_whitespace():
    assert normalize_email("  user@example.com  ") == "user@example.com"


def test_nfkc_normalizes_unicode_variants():
    # U+FF21 (fullwidth "A") NFKC-normalizes to U+0041 ("A") — without this, two visually
    # near-identical addresses could both pass the format check and register as "different"
    # rows despite being the same real-world address to a human reader.
    fullwidth = "ａｂｃ@example.com"  # fullwidth "abc@example.com"
    assert normalize_email(fullwidth) == "abc@example.com"


def test_idempotent():
    once = normalize_email("User@Example.com")
    twice = normalize_email(once)
    assert once == twice
