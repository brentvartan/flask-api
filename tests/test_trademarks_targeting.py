"""
Tests for the 2026-07 trademark targeting redesign (fresh sort + owner-quality
filter + bounded pagination). Pure-function tests — no network.
"""
from app.services import trademarks as tm


def test_brand_llc_owners_are_kept():
    # A new brand entity filing as LLC/Inc/Corp must NOT be rejected.
    for owner in ["Rivalz Inc.", "Fascent LLC", "Algae Cooking Club, Inc.",
                  "Harken Sweets Corp", "Bubble Skincare LLC"]:
        assert tm._is_brand_candidate(owner, "SomeMark"), f"wrongly rejected: {owner}"


def test_institutional_owners_are_rejected():
    for owner in ["Blackstone Capital Partners LP", "Sequoia Holdings LLC",
                  "First National Bank", "Stanford University",
                  "Smith & Jones Attorneys LLP", "Acme Property Management Inc",
                  "Global Logistics Ventures"]:
        assert not tm._is_brand_candidate(owner, "SomeMark"), f"should reject: {owner}"


def test_empty_wordmark_rejected():
    assert not tm._is_brand_candidate("Rivalz Inc", "")
    assert not tm._is_brand_candidate("Rivalz Inc", "   ")


def test_capital_in_brand_name_still_filtered_but_llc_never_triggers():
    # "Capital" as a real word triggers reject; the LLC suffix alone never does.
    assert not tm._is_brand_candidate("Capital Foods LLC", "Mark")   # 'capital' word
    assert tm._is_brand_candidate("Graza LLC", "Mark")               # only suffix


def test_query_is_sorted_by_filed_date_desc():
    from datetime import datetime, timedelta
    q = tm._es_query(datetime(2026, 6, 1), datetime(2026, 7, 1), 100, 0)
    assert q["sort"] == [{"filedDate": {"order": "desc"}}]
    assert q["from"] == 0 and q["size"] == 100
    assert q["query"]["bool"]["minimum_should_match"] == 1
