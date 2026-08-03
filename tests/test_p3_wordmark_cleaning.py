"""
A design description is not a brand name.

USPTO sometimes fills the wordmark field with the design description instead of
the mark. Found in calibration 2026-08-03: Brent rejected a brand scoring 72
whose thesis read "the founder-voiced Southern-identity seasoning brand that
McCormick can never be — a Fly by Jing moment for the American South", because
the name on his card was sixty characters of trademark-office boilerplate.

He judged what he was shown. A presentation failure read as a judgement.
"""
import pytest

from app.services.trademarks import _clean_wordmark


@pytest.mark.parametrize("raw,expected", [
    ('THE MARK CONSISTS OF THE WORDING "SHELL SHOK" ABOVE A CRACKED EGG', "SHELL SHOK"),
    ("THE MARK CONSISTS OF THE STYLIZED WORDING 'ARIOO' WITH A LETTER O", "ARIOO"),
    ('THE MARK CONSISTS OF THE STYLIZED WORDING "BOURBON ROYALTY" IN GOLD',
     "BOURBON ROYALTY"),
    ('the mark consists of the wording "lowercase works" too', "lowercase works"),
])
def test_the_real_mark_is_recovered(raw, expected):
    assert _clean_wordmark(raw) == expected


@pytest.mark.parametrize("raw", [
    "OLIPOP",
    "Fly by Jing",
    "SHELL SHOK",
])
def test_ordinary_wordmarks_are_untouched(raw):
    assert _clean_wordmark(raw) == raw


def test_a_description_with_no_quoted_mark_is_left_alone():
    """
    Better an ugly name than a wrong one — if there is nothing quoted to
    recover, do not invent a brand.
    """
    raw = "THE MARK CONSISTS OF INDIAN SKULL WITH FEATHERHEAD AND HEADDRESS"
    assert _clean_wordmark(raw) == raw


def test_single_letters_are_not_mistaken_for_a_brand():
    """'THE LARGE STYLIZED SERIF LETTERS "E", "B"' has quotes but no name."""
    raw = 'THE MARK CONSISTS OF THE LARGE STYLIZED SERIF LETTERS "E", "B"'
    assert _clean_wordmark(raw) == raw


@pytest.mark.parametrize("raw", [None, ""])
def test_empty_input_is_safe(raw):
    assert _clean_wordmark(raw) == raw
