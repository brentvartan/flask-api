"""
Task 1 tests — Form D related persons inline + watchlist match.

All tests use fixture XML; no live network calls.
"""
import json
import types
import pytest
from unittest.mock import patch, MagicMock

# ── Fixture EDGAR XML ─────────────────────────────────────────────────────────
# Minimal Form D XML with two related persons.
# Person 1: a real founder on the watchlist_seed.csv conviction list (first
#   CSV row with designation=founder — loaded dynamically so the test stays
#   in sync with the seed file).
# Person 2: a known alumni also in the seed.
# We load the first founder/alumni from the CSV so the test doesn't hard-code
# a name that could be removed from the seed.

import csv, os as _os

_CSV = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), '..', 'data', 'watchlist_seed.csv')
)

def _first_by_designation(d):
    with open(_CSV, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('designation','').strip().lower() == d and row.get('name','').strip():
                return row['name'].strip()
    return None

_CONVICTION_NAME = _first_by_designation('founder')
_ALUMNI_NAME     = _first_by_designation('alumni')

assert _CONVICTION_NAME, "No founder rows in watchlist_seed.csv — fix the seed"
assert _ALUMNI_NAME,     "No alumni rows in watchlist_seed.csv — fix the seed"

def _make_xml(person1_first, person1_last, person2_first, person2_last):
    return f"""<?xml version="1.0"?>
<edgarSubmission>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>{person1_first}</firstName>
        <lastName>{person1_last}</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>{person2_first}</firstName>
        <lastName>{person2_last}</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
</edgarSubmission>
""".encode()


def _split_name(full):
    parts = full.strip().split()
    return parts[0], parts[-1]


_P1_FIRST, _P1_LAST = _split_name(_CONVICTION_NAME)
_P2_FIRST, _P2_LAST = _split_name(_ALUMNI_NAME)

_FIXTURE_XML = _make_xml(_P1_FIRST, _P1_LAST, _P2_FIRST, _P2_LAST)

# XML with one totally unknown person (not in seed)
_UNKNOWN_XML = _make_xml("Zxzx", "Qqqq", "Aaaa", "Bbbb")


# ── fetch_form_d_related_persons tests ───────────────────────────────────────

from app.services.delaware import fetch_form_d_related_persons

class _FakeResp:
    status_code = 200
    def __init__(self, content): self.content = content

def _mock_get(xml_bytes):
    def _get(*args, **kwargs):
        return _FakeResp(xml_bytes)
    return _get


def test_fetch_related_persons_parses_names():
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        persons = fetch_form_d_related_persons("0001234567-24-000001", "1234567")
    assert len(persons) == 2
    names = [p['name'] for p in persons]
    assert _CONVICTION_NAME in names
    assert _ALUMNI_NAME in names


def test_fetch_related_persons_parses_relationships():
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        persons = fetch_form_d_related_persons("0001234567-24-000001", "1234567")
    p1 = next(p for p in persons if _CONVICTION_NAME in p['name'])
    assert "Executive Officer" in p1['relationships']
    assert "Director" in p1['relationships']


def test_fetch_related_persons_empty_on_bad_xml():
    with patch('requests.get', side_effect=_mock_get(b"not xml at all")):
        persons = fetch_form_d_related_persons("0001234567-24-000002", "1234567")
    assert persons == []


def test_fetch_related_persons_empty_on_http_error():
    bad_resp = MagicMock()
    bad_resp.status_code = 404
    with patch('requests.get', return_value=bad_resp):
        persons = fetch_form_d_related_persons("0001234567-24-000003", "1234567")
    assert persons == []


# ── _enrich_related_persons tests ────────────────────────────────────────────

from app.services.delaware import _enrich_related_persons


def _make_signal(adsh="0001234567-24-000001", cik="1234567"):
    return {
        "companyName": "Test Brand",
        "signal_type": "delaware",
        "_adsh": adsh,
        "_cik":  cik,
        "_name": "Test Brand LLC",
    }


def test_enrich_attaches_related_persons():
    sig = _make_signal()
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        _enrich_related_persons([sig], limit=1)
    assert "related_persons" in sig
    assert len(sig["related_persons"]) == 2


def test_enrich_sets_filer_name():
    sig = _make_signal()
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        _enrich_related_persons([sig], limit=1)
    # filer_name should be the first person-looking name
    assert "filer_name" in sig
    assert sig["filer_name"] in (_CONVICTION_NAME, _ALUMNI_NAME)


def test_enrich_flags_conviction_match():
    sig = _make_signal()
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        _enrich_related_persons([sig], limit=1)
    matches = [
        rp for rp in sig["related_persons"]
        if rp.get("conviction_match")
    ]
    assert len(matches) >= 1
    assert matches[0]["conviction_match"]["name"] == _CONVICTION_NAME
    assert matches[0]["conviction_match"]["designation"] == "founder"


def test_enrich_flags_alumni_match():
    sig = _make_signal()
    with patch('requests.get', side_effect=_mock_get(_FIXTURE_XML)):
        _enrich_related_persons([sig], limit=1)
    matches = [
        rp for rp in sig["related_persons"]
        if rp.get("exit_alumni_match")
    ]
    assert len(matches) >= 1
    assert matches[0]["exit_alumni_match"]["designation"] == "alumni"


def test_enrich_no_match_for_unknown_person():
    sig = _make_signal()
    with patch('requests.get', side_effect=_mock_get(_UNKNOWN_XML)):
        _enrich_related_persons([sig], limit=1)
    for rp in sig["related_persons"]:
        assert rp["conviction_match"] is None
        assert rp["exit_alumni_match"] is None


def test_enrich_respects_limit():
    sigs = [_make_signal(adsh=f"000100000{i}-24-000001", cik=str(i)) for i in range(5)]
    call_count = 0
    def counting_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeResp(_FIXTURE_XML)
    with patch('requests.get', side_effect=counting_get):
        _enrich_related_persons(sigs, limit=2)
    # Only the first 2 signals should have triggered an EDGAR fetch
    assert call_count == 2
    assert "related_persons" in sigs[0]
    assert "related_persons" in sigs[1]
    assert "related_persons" not in sigs[2]


def test_enrich_skips_signal_without_adsh():
    sig = {"companyName": "No Adsh Brand", "signal_type": "delaware", "_cik": "1234"}
    _enrich_related_persons([sig], limit=1)  # no requests.get mock — should not call it
    assert "related_persons" not in sig


# ── filer_name → SerpAPI skip integration ────────────────────────────────────

def test_filer_name_skips_serp_api():
    """When filer_name is a person-looking name, discover_founder returns high-confidence
    without hitting SerpAPI."""
    from app.services.founder_discovery import discover_founder
    with patch('app.services.founder_discovery._serp_search') as mock_serp:
        result = discover_founder("TestBrand", filer_name=_CONVICTION_NAME, category="CPG")
    # SerpAPI must NOT have been called
    mock_serp.assert_not_called()
    assert result["name"] == _CONVICTION_NAME
    assert result["confidence"] == "high"
