"""
Task 3 tests — Form D raise amount from XML.

All tests use fixture XML bytes. No live network calls.
Offering data is a RANKING FEATURE ONLY — never used to gate signals.
"""
import pytest
from unittest.mock import patch

from app.services.delaware import (
    parse_form_d_offering_data,
    _enrich_related_persons,
    _fetch_form_d_xml_content,
    _is_consumer_candidate,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

_XML_WITH_OFFERING = b"""<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <entityName>Testbrand LLC</entityName>
  </primaryIssuer>
  <offeringData>
    <federalExemptionsExclusions>
      <item>06b</item>
    </federalExemptionsExclusions>
    <minimumInvestmentAccepted>25000</minimumInvestmentAccepted>
    <offeringSalesAmounts>
      <totalOfferingAmount>1000000</totalOfferingAmount>
      <totalAmountSold>500000</totalAmountSold>
      <totalRemaining>500000</totalRemaining>
    </offeringSalesAmounts>
  </offeringData>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Jane</firstName>
        <lastName>Founder</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
</edgarSubmission>
"""

_XML_INDEFINITE_OFFERING = b"""<?xml version="1.0"?>
<edgarSubmission>
  <offeringData>
    <minimumInvestmentAccepted>0</minimumInvestmentAccepted>
    <offeringSalesAmounts>
      <totalOfferingAmount indefiniteAmount="true">0</totalOfferingAmount>
      <totalAmountSold>0</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>
"""

_XML_NO_OFFERING = b"""<?xml version="1.0"?>
<edgarSubmission>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Bob</firstName>
        <lastName>Tester</lastName>
      </relatedPersonName>
    </relatedPersonInfo>
  </relatedPersonsList>
</edgarSubmission>
"""

_XML_PARTIAL_OFFERING = b"""<?xml version="1.0"?>
<edgarSubmission>
  <offeringData>
    <offeringSalesAmounts>
      <totalOfferingAmount>250000</totalOfferingAmount>
      <totalAmountSold>0</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>
"""


# ── parse_form_d_offering_data ────────────────────────────────────────────────

def test_parses_total_offering():
    result = parse_form_d_offering_data(_XML_WITH_OFFERING)
    assert result is not None
    assert result["total_offering"] == 1_000_000


def test_parses_amount_sold():
    result = parse_form_d_offering_data(_XML_WITH_OFFERING)
    assert result["amount_sold"] == 500_000


def test_parses_minimum_investment():
    result = parse_form_d_offering_data(_XML_WITH_OFFERING)
    assert result["minimum_investment"] == 25_000


def test_indefinite_offering_returns_none_for_total():
    result = parse_form_d_offering_data(_XML_INDEFINITE_OFFERING)
    # total_offering and amount_sold are 0 → None; minimum_investment 0 → None
    # All None → function should return None (no meaningful data)
    assert result is None


def test_no_offering_section_returns_none():
    result = parse_form_d_offering_data(_XML_NO_OFFERING)
    assert result is None


def test_partial_offering_returns_non_zero_fields():
    result = parse_form_d_offering_data(_XML_PARTIAL_OFFERING)
    assert result is not None
    assert result["total_offering"] == 250_000
    # totalAmountSold = 0 → treated as None (not yet sold)
    assert result["amount_sold"] is None


def test_bad_xml_returns_none():
    result = parse_form_d_offering_data(b"not xml at all <<<")
    assert result is None


def test_empty_bytes_returns_none():
    result = parse_form_d_offering_data(b"")
    assert result is None


# ── _enrich_related_persons attaches offering data ───────────────────────────

class _FakeResp:
    status_code = 200
    def __init__(self, content): self.content = content


def test_enrich_attaches_offering_to_signal():
    sig = {
        "companyName": "Testbrand",
        "signal_type": "delaware",
        "_adsh": "0001234567-24-000001",
        "_cik":  "1234567",
        "_name": "Testbrand LLC",
    }
    with patch('requests.get', return_value=_FakeResp(_XML_WITH_OFFERING)):
        _enrich_related_persons([sig], limit=1)

    assert sig.get("total_offering") == 1_000_000
    assert sig.get("amount_sold") == 500_000
    assert sig.get("minimum_investment") == 25_000


def test_enrich_still_attaches_persons_alongside_offering():
    sig = {
        "companyName": "Testbrand",
        "signal_type": "delaware",
        "_adsh": "0001234567-24-000001",
        "_cik":  "1234567",
        "_name": "Testbrand LLC",
    }
    with patch('requests.get', return_value=_FakeResp(_XML_WITH_OFFERING)):
        _enrich_related_persons([sig], limit=1)

    assert "related_persons" in sig
    assert any(p["name"] == "Jane Founder" for p in sig["related_persons"])
    assert sig["total_offering"] == 1_000_000


def test_enrich_no_offering_section_leaves_signal_clean():
    """Signal with no offeringData section must not have offering keys added."""
    sig = {
        "companyName": "Testbrand",
        "signal_type": "delaware",
        "_adsh": "0001234567-24-000002",
        "_cik":  "1234567",
        "_name": "Testbrand LLC",
    }
    with patch('requests.get', return_value=_FakeResp(_XML_NO_OFFERING)):
        _enrich_related_persons([sig], limit=1)

    assert "total_offering" not in sig
    assert "amount_sold" not in sig


# ── Raise size is never a gate ────────────────────────────────────────────────

def test_is_consumer_candidate_ignores_raise_size():
    """_is_consumer_candidate must not filter on raise amount — raise size is a rank feature."""
    import inspect
    source = inspect.getsource(_is_consumer_candidate)
    # Confirm neither "total_offering" nor "raise" appears as a filter condition
    assert "total_offering" not in source
    assert "amount_sold" not in source
    # Should still pass regardless of what we might add later:
    assert _is_consumer_candidate("Coolbrand", []) is True
