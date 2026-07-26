"""
Tier-floor behaviour for genuine legal signals.

The floor exists so a real trademark or Form D filing is not buried as COLD by a
model reading a sparse record. But the condition was `has_trademark or
has_form_d` — trivially true for EVERY trademark. It was dead code until P1
started passing signal_types, and measured against the live corpus it would have
promoted 88% of scored signals to WARM, including ones that scored 0 for failing
the consumer gate. These tests pin the narrower rule.
"""
import json
from unittest.mock import patch

import app.services.enrichment as en


def _fake_claude(score, level="cold"):
    payload = json.dumps({"bullish_score": score, "watch_level": level})

    class _Msg:
        content = [type("T", (), {"text": payload})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _Msg()

    return _Client()


def _enrich(score, signal_types, conviction=None):
    with patch.object(en, "_get_client", return_value=_fake_claude(score)):
        return en.enrich_signal({
            "companyName": "Testbrand",
            "category": "Beauty",
            "signal_type": "trademark",
            "description": "d",
            "notes": "n",
            "signal_types": signal_types,
            "signal_count": len(signal_types),
            "conviction_match": conviction,
        })


def test_gate_failure_is_never_floored():
    """A 0 means the consumer gate rejected it — B2B, ad-supported, already big."""
    assert _enrich(0, ["trademark"])["watch_level"] == "cold"


def test_clearly_uninteresting_single_signal_stays_cold():
    assert _enrich(12, ["trademark"])["watch_level"] == "cold"
    assert _enrich(34, ["trademark"])["watch_level"] == "cold"


def test_near_miss_single_signal_is_floored_to_warm():
    r = _enrich(47, ["trademark"])
    assert r["watch_level"] == "warm"
    assert r["tier_floor_reason"] == "trademark_near_miss"


def test_near_miss_boundary_is_inclusive():
    assert _enrich(en._TIER_FLOOR_NEAR_MISS, ["trademark"])["watch_level"] == "warm"


def test_triangulation_is_floored_at_any_score():
    """Trademark AND Form D is the core edge — it outranks a sparse-record read."""
    r = _enrich(8, ["trademark", "delaware"])
    assert r["watch_level"] == "warm"
    assert r["tier_floor_reason"] == "tm_plus_form_d"


def test_conviction_match_still_forces_hot():
    r = _enrich(3, ["trademark"], conviction={"name": "Cora Founder"})
    assert r["watch_level"] == "hot"
    assert r["tier_floor_reason"] == "conviction_match"


def test_already_warm_or_hot_is_left_alone():
    with patch.object(en, "_get_client", return_value=_fake_claude(72, "hot")):
        r = en.enrich_signal({"companyName": "X", "signal_types": ["trademark"]})
    assert r["watch_level"] == "hot"
    assert "tier_floor_reason" not in r


def test_floor_is_inert_without_signal_types():
    """Callers that do not pass signal_types must not get a silent promotion."""
    assert _enrich(47, [])["watch_level"] == "cold"
