"""
The weekly list is ordered by learned theme affinity, not by score.

Brent's 2026-08-03 triage (8 rounds of "pick 3 of 10", 24 picks):
  • his top-3 overlapped the model's top 3 on 7 of 24 — random is 30%
  • his picks averaged +0.1 points above the round mean
  • he took 3/3 GLP-1 brands and 0/6 Third-Place Fitness, spanning scores 48-74

So score decides MEMBERSHIP (100% agreement on gate failures, ~93% above 45)
and decides ORDER not at all. Sorting the digest by score would have been
indistinguishable from shuffling it.
"""
import json

import pytest

from app.models.item import Item
from app.services.ranking import (
    DEFAULT_THEME_WEIGHTS,
    MIN_WEIGHT,
    UNTHEMED,
    canonical_themes,
    load_theme_weights,
    rank_signals,
    rank_value,
    theme_weight,
)


class TestCanonicalisation:
    @pytest.mark.parametrize("raw,expect", [
        ("GLP-1 / Weight Management Adjacent", "GLP-1 / Weight Management"),
        ("Longevity & Healthspan", "Longevity / Healthspan"),
        ("Third-Place Fitness / Longevity & Healthspan", "Longevity / Healthspan"),
        ("Premium Pet", "Premium Pet"),
    ])
    def test_free_text_themes_map_to_canonical_buckets(self, raw, expect):
        assert expect in canonical_themes(raw)

    def test_model_prose_does_not_create_a_new_theme(self):
        """
        Live output included 'Lifestyle Identity (resort', 'leisure as identity)'
        and 'Healthspan — with a Western Herbalism' — naive splitting produced 31
        fragmented buckets from 12 real themes.
        """
        assert canonical_themes("Lifestyle Identity (resort, leisure as identity)") \
            == {UNTHEMED}

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_missing_theme_is_unthemed_not_an_error(self, raw):
        assert canonical_themes(raw) == {UNTHEMED}


class TestWeightsRankButNeverGate:
    def test_no_default_weight_is_zero(self):
        """A zero weight would turn ranking into filtering — invariant 4."""
        assert all(w >= MIN_WEIGHT for w in DEFAULT_THEME_WEIGHTS.values())
        assert MIN_WEIGHT > 0

    def test_a_zero_pick_theme_is_lowered_not_erased(self):
        """He picked 0 of 6 Third-Place Fitness. It still ranks above nothing."""
        w = DEFAULT_THEME_WEIGHTS["Third-Place Fitness"]
        assert 0 < w < 1.0

    def test_ranking_never_drops_an_entry(self):
        entries = [
            {"name": "A", "score": 74, "theme": "Third-Place Fitness"},
            {"name": "B", "score": 48, "theme": "GLP-1 / Weight Management Adjacent"},
            {"name": "C", "score": 60, "theme": None},
        ]
        out = rank_signals(entries, DEFAULT_THEME_WEIGHTS)
        assert len(out) == len(entries)
        assert {e["name"] for e in out} == {"A", "B", "C"}


class TestOrderingBehaviour:
    def test_a_favoured_theme_can_outrank_a_higher_score(self):
        """
        The whole point: he took WEIGHLESS (72, GLP-1) AND SHRED THEORY (52,
        GLP-1) while passing over higher-scoring Third-Place Fitness brands.
        """
        entries = [
            {"name": "fitness studio", "score": 68, "theme": "Third-Place Fitness"},
            {"name": "glp1 brand",     "score": 52, "theme": "GLP-1 / Weight Management"},
        ]
        out = rank_signals(entries, DEFAULT_THEME_WEIGHTS)
        assert out[0]["name"] == "glp1 brand"

    def test_score_still_orders_within_a_theme(self):
        entries = [
            {"name": "lower", "score": 52, "theme": "Premium Pet"},
            {"name": "higher", "score": 71, "theme": "Premium Pet"},
        ]
        out = rank_signals(entries, DEFAULT_THEME_WEIGHTS)
        assert [e["name"] for e in out] == ["higher", "lower"]

    def test_a_brand_takes_the_best_of_its_themes(self):
        """Being also-tagged Analog Revival must not punish a GLP-1 brand."""
        both = theme_weight("GLP-1 / Weight Management / Analog Revival",
                            DEFAULT_THEME_WEIGHTS)
        assert both == DEFAULT_THEME_WEIGHTS["GLP-1 / Weight Management"]

    def test_missing_score_does_not_crash_the_digest(self):
        assert rank_value(None, "Premium Pet", DEFAULT_THEME_WEIGHTS) == 0.0
        assert rank_value("nonsense", "Premium Pet", DEFAULT_THEME_WEIGHTS) == 0.0


class TestWeightsAreAStatedPreference:
    def test_stored_weights_override_defaults(self, db, admin_user):
        db.session.add(Item(
            title="__bullish_settings__", item_type="settings", owner_id=admin_user.id,
            description=json.dumps({"_type": "settings",
                                    "theme_weights": {"Third-Place Fitness": 2.0}})))
        db.session.commit()
        w = load_theme_weights()
        assert w["Third-Place Fitness"] == 2.0
        # untouched themes keep their defaults
        assert w["GLP-1 / Weight Management"] == \
            DEFAULT_THEME_WEIGHTS["GLP-1 / Weight Management"]

    def test_a_stored_zero_is_clamped_not_honoured(self, db, admin_user):
        """Even an explicit 0 must not become a filter."""
        db.session.add(Item(
            title="__bullish_settings__", item_type="settings", owner_id=admin_user.id,
            description=json.dumps({"_type": "settings",
                                    "theme_weights": {"GenAlpha Beauty": 0}})))
        db.session.commit()
        assert load_theme_weights()["GenAlpha Beauty"] >= MIN_WEIGHT

    def test_garbage_stored_weight_is_ignored(self, db, admin_user):
        db.session.add(Item(
            title="__bullish_settings__", item_type="settings", owner_id=admin_user.id,
            description=json.dumps({"_type": "settings",
                                    "theme_weights": {"Premium Pet": "high"}})))
        db.session.commit()
        assert load_theme_weights()["Premium Pet"] == DEFAULT_THEME_WEIGHTS["Premium Pet"]
