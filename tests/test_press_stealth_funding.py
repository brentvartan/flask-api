"""
Tests for the 2026-07 inbox-audit coverage fix: the funding-in-trade-press
detector added to press_stealth.py. These use the ACTUAL headlines of brands
the scanner missed in the July 2026 audit, so a regression here means the audit
gap has reopened.

Pure-function tests — no Flask app or DB required.
"""
from app.services import press_stealth as ps


def _consumer(text: str) -> bool:
    return bool(ps._CONSUMER_RE.search(text))


def _funding(text: str) -> bool:
    return bool(ps._FUNDING_RE.search(text))


def _stealth(text: str) -> bool:
    return bool(ps._STEALTH_RE.search(text))


# ── Real missed-brand headlines should now match the funding detector ─────────

MISSED_HEADLINES = [
    ("Rivalz",             "Rivalz nets $5M, launches puff snack"),
    ("Fascent",            "French fragrance brand Fascent raises funding to fuel growth"),
    ("Algae Cooking Club", "Algae Cooking Club secured $11.6M in fresh funding"),
    ("HaloBraid",          "Hair-tech startup HaloBraid raises $7M seed round for braiding robot"),
    ("B-Sides",            "B-Sides raises $500,000 in seed round for upcycled snack puffs"),
    ("Harken Sweets",      "Better-for-you candy brand Harken Sweets lands new seed investment"),
]


def test_missed_funding_headlines_now_match():
    for _brand, headline in MISSED_HEADLINES:
        assert _funding(headline), f"funding pattern missed: {headline!r}"
        assert _consumer(headline), f"consumer context missed: {headline!r}"


def test_funding_brand_extraction():
    """The extractor now reports (name, confident) — see _extract_brand_hint."""
    for brand, headline in MISSED_HEADLINES:
        got, confident = ps._extract_funding_brand(headline, "")
        assert brand.lower().split()[0] in got.lower(), (
            f"expected brand containing {brand!r}, got {got!r} from {headline!r}"
        )
        assert confident, f"a real brand extraction must be confident: {headline!r}"


def test_a_headline_with_no_identifiable_brand_is_not_confident():
    """
    The fallback returns the article title. Those reached the weekly digest
    presented as brand names — two of ten entries on 2026-07-27. They must be
    marked so the digest can leave them out; they stay on the dashboard.
    """
    for headline in (
        "Snack Bars Were Never Refreshing. Until Now.",
        "Distribution: Gymkhana, Kreatures of Habit head to Sprouts; more news",
    ):
        _, confident = ps._extract_funding_brand(headline, "")
        assert not confident, f"headline should not read as a brand: {headline!r}"


def test_a_brand_leading_the_headline_is_still_confident():
    """
    Guards against the heuristic I tried first: inferring uncertainty from
    "the name is a prefix of the title" wrongly flagged 'Rivalz nets $5M',
    which is the COMMON shape of a funding headline.
    """
    name, confident = ps._extract_funding_brand("Rivalz nets $5M seed round", "")
    assert name == "Rivalz" and confident


# ── Precision guards: don't fire on non-funding or B2B ────────────────────────

def test_no_funding_language_does_not_match():
    # Pure trend/news commentary — no raise mentioned.
    assert not _funding("Cottage cheese is getting protein-maxxed on TikTok")
    assert not _funding("Reformation's IPO filing shows profitable DTC is still possible")


def test_b2b_raise_excluded_by_title_gate():
    # The B2B title gate must veto even a consumer-adjacent enterprise raise.
    title = "Enterprise SaaS platform raises $50M Series B"
    assert ps._B2B_RE.search(title), "B2B gate should catch this title"


def test_stealth_detector_still_works():
    # The original stealth path must be unaffected.
    headline = "Ex-Glossier exec left to build something new in beauty"
    assert _stealth(headline)
    assert _consumer(headline)


def test_stealth_takes_precedence_over_funding_label():
    # A headline that is both stealth AND funding should keep the stealth brand
    # extractor path (handled in _fetch_feed); here we just assert both fire so
    # the precedence branch is exercised.
    headline = 'Stealth consumer startup "Lumen" raises $3M seed'
    assert _stealth(headline) and _funding(headline)
