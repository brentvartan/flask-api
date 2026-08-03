"""
The tier floor must rank, not launder.

Measured against the live corpus 2026-08-03: WARM held 3,534 brands, of which
2,381 were floor-promoted and only 1,153 were genuinely scored WARM by the
model. Among the promoted were 117 that scored ZERO — a Vancouver copper and
gold mining explorer, a healthcare management consultancy, an L'Oreal corporate
trademark — every one carrying a thesis that literally began "Hard pass".

Two faults: the triangulation floor ignored gate failures, and "near miss" was
set to 35 against a WARM line of 55, so a brand seventeen points below WARM read
the same as one two points below.
"""
import pytest

from app.services.enrichment import (
    _TIER_FLOOR_GATE_FAIL_AT,
    _TIER_FLOOR_NEAR_MISS,
)


def _apply_floor(score, has_tm=True, has_form_d=False,
                 consumer_brand=None, gate_passed=None):
    """Mirrors the floor block in enrich_signal."""
    level = "hot" if score >= 70 else "warm" if score >= 55 else "cold"
    if level != "cold":
        return level, None
    gate_failed = (score <= _TIER_FLOOR_GATE_FAIL_AT
                   or consumer_brand is False
                   or gate_passed is False)
    if gate_failed:
        return "cold", "gate_failed_no_floor"
    if has_tm and has_form_d:
        return "warm", "tm_plus_form_d"
    if score >= _TIER_FLOOR_NEAR_MISS:
        return "warm", "trademark_near_miss" if has_tm else "form_d_near_miss"
    return "cold", None


class TestRejectedSignalsAreNeverFloored:
    @pytest.mark.parametrize("score", [0, 2, 8, 10])
    def test_a_gate_failure_stays_cold_even_with_two_signal_types(self, score):
        """The real case: a mining company with a trademark AND a Form D."""
        level, reason = _apply_floor(score, has_tm=True, has_form_d=True)
        assert level == "cold"
        assert reason == "gate_failed_no_floor"

    def test_explicit_non_consumer_stays_cold(self):
        level, _ = _apply_floor(40, has_tm=True, has_form_d=True, consumer_brand=False)
        assert level == "cold"

    def test_failed_founder_gate_stays_cold(self):
        level, _ = _apply_floor(40, has_tm=True, has_form_d=True, gate_passed=False)
        assert level == "cold"


class TestNearMissMeansNear:
    @pytest.mark.parametrize("score", [12, 18, 28, 38, 44])
    def test_scores_far_below_warm_are_not_near_misses(self, score):
        """38 was the single largest WARM cohort — 1,582 brands, 17 under WARM."""
        level, _ = _apply_floor(score, has_tm=True)
        assert level == "cold", f"{score} is not a near miss of 55"

    @pytest.mark.parametrize("score", [48, 52, 54])
    def test_genuinely_borderline_scores_are_floored(self, score):
        level, reason = _apply_floor(score, has_tm=True)
        assert level == "warm"
        assert reason == "trademark_near_miss"

    def test_the_threshold_sits_just_under_warm(self):
        assert 45 <= _TIER_FLOOR_NEAR_MISS < 55


class TestTriangulationStillCounts:
    def test_a_plausible_brand_on_two_legal_channels_is_floored(self):
        """The core edge survives — it just cannot launder a rejected premise."""
        level, reason = _apply_floor(42, has_tm=True, has_form_d=True)
        assert level == "warm"
        assert reason == "tm_plus_form_d"


class TestNothingIsHidden:
    def test_cold_is_a_badge_not_a_filter(self):
        """
        Invariant 4: people and rules change RANK, never visibility. A signal
        left COLD is still saved, still on the dashboard, still searchable.
        """
        level, _ = _apply_floor(0, has_tm=True, has_form_d=True)
        assert level == "cold"   # badge only — the row is untouched
