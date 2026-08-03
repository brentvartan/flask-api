"""
Bullish Stealth Finder — weekly list ordering.

WHY THIS EXISTS
Brent labelled 80 brands blind on 2026-08-03, then triaged 8 rounds of 10
("which three would you open first"). Two results came out of it:

  1. bullish_score decides membership well. Agreement with his yes/no was 100%
     on gate failures and ~93% on everything above 45.

  2. bullish_score decides ORDER not at all. His top-3 picks overlapped the
     model's top 3 on 7 of 24 — random is 30% — and his picks averaged +0.1
     points above the round mean. Sorting the weekly list by score would be
     indistinguishable from shuffling it.

What DID predict his picks was the cultural theme. He took every GLP-1 brand
offered (3/3) and none of Third-Place Fitness (0/6), Climate-Positive (0/5) or
AI-Personalized Care (0/3) — spanning scores 48 to 74 either way.

DESIGN CONSTRAINTS

  • Weights RANK, they never GATE. The lowest weight still surfaces, just lower
    (invariant 4: people and rules change rank, never visibility). Nothing here
    can remove a brand from the list.

  • Weights are SHRUNK toward neutral. 24 picks across 12 themes is thin
    evidence; 0/6 is not proof a theme is worthless. A Bayesian-style prior
    keeps a 0% theme at 0.50 rather than 0, so a category Brent has said the
    fund cares about cannot silently vanish because of one quiet fortnight.

  • Weights are VISIBLE and EDITABLE, stored in __bullish_settings__ under
    "theme_weights" and surfaced by GET/PUT /api/admin/theme-weights. This is a
    STATED PREFERENCE, not a model that quietly learned something nobody
    intended. If the fund decides Third-Place Fitness matters, that is a number
    a human changes — not a retraining exercise.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

SETTINGS_KEY = "theme_weights"

# The twelve 2026 cultural themes named in enrichment.SYSTEM_PROMPT, with the
# substrings the model actually emits. Free-text themes fragment badly —
# "Lifestyle Identity (resort", "leisure as identity)" — so everything is
# canonicalised into these buckets before weighting.
CANONICAL_THEMES = {
    "GLP-1 / Weight Management":  ["glp-1", "glp1", "weight management"],
    "Women's Health Renaissance": ["women's health", "womens health", "perimenopause",
                                   "menopause", "fertility", "period care"],
    "Longevity / Healthspan":     ["longevity", "healthspan", "biological age", "nad+"],
    "Functional Beverages":       ["functional beverage", "adaptogenic", "nootropic"],
    "Men's Personal Care":        ["men's personal care", "mens personal care"],
    "Third-Place Fitness":        ["third-place fitness", "third place fitness"],
    "GenAlpha Beauty":            ["genalpha", "gen alpha"],
    "Premium Pet":                ["premium pet"],
    "Analog Revival":             ["analog revival"],
    "Dietary / Food Identity":    ["dietary", "food identity", "food as medicine",
                                   "regenerative"],
    "Climate-Positive Consumer":  ["climate-positive", "climate positive"],
    "AI-Personalized Care":       ["ai-personalized", "ai personalized"],
}

UNTHEMED = "Other / unthemed"

# Derived from the 2026-08-03 triage, shrunk toward 1.0 (neutral) with a prior
# weight of 6 observations. Editable at runtime — these are only the starting
# point, and the stored settings win.
DEFAULT_THEME_WEIGHTS = {
    "GLP-1 / Weight Management":  1.55,
    "Longevity / Healthspan":     1.45,
    "Premium Pet":                1.14,
    "Functional Beverages":       1.03,
    "Women's Health Renaissance": 1.00,
    "Dietary / Food Identity":    0.98,
    "Men's Personal Care":        0.97,
    "Analog Revival":             0.85,
    "GenAlpha Beauty":            0.74,
    "AI-Personalized Care":       0.67,
    "Climate-Positive Consumer":  0.55,
    "Third-Place Fitness":        0.50,
    UNTHEMED:                     1.00,
}

# A weight may never reach zero, or ranking would become gating.
MIN_WEIGHT, MAX_WEIGHT = 0.25, 3.0


def canonical_themes(raw: str) -> set:
    """Map a free-text cultural_theme onto the canonical buckets."""
    text = (raw or "").lower()
    hits = {name for name, pats in CANONICAL_THEMES.items()
            if any(p in text for p in pats)}
    return hits or {UNTHEMED}


def load_theme_weights() -> dict:
    """
    Stored weights merged over the defaults, so a partial edit is safe and a new
    theme added to the code still has a sensible starting value.
    """
    from ..models.item import Item

    weights = dict(DEFAULT_THEME_WEIGHTS)
    try:
        row = Item.query.filter_by(title="__bullish_settings__").first()
        if row:
            stored = (json.loads(row.description or "{}") or {}).get(SETTINGS_KEY) or {}
            for k, v in stored.items():
                try:
                    weights[k] = max(MIN_WEIGHT, min(float(v), MAX_WEIGHT))
                except (TypeError, ValueError):
                    logger.warning("Ignoring non-numeric theme weight %r=%r", k, v)
    except Exception as exc:
        logger.warning("Theme weight lookup failed (%s) — using defaults", exc)
    return weights


def theme_weight(raw_theme: str, weights: dict = None) -> float:
    """
    Weight for a signal's theme string. A brand carrying several themes takes
    the BEST of them — one strong theme is enough to earn attention, and
    averaging would punish a GLP-1 brand for also being tagged Analog Revival.
    """
    weights = weights or load_theme_weights()
    return max((weights.get(t, 1.0) for t in canonical_themes(raw_theme)), default=1.0)


def rank_value(score, raw_theme: str, weights: dict = None) -> float:
    """
    The ordering key. Score still participates — it is a fine tiebreak WITHIN a
    theme, it simply carries no signal ACROSS themes. Multiplying keeps both:
    theme sets the band, score orders inside it.
    """
    try:
        base = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        base = 0.0
    return base * theme_weight(raw_theme, weights)


def rank_signals(entries: list, weights: dict = None) -> list:
    """
    Order a list of digest entries, highest first.

    Each entry is a dict with "score" and "theme". Returns a NEW list; nothing
    is filtered out — every entry that went in comes out, only reordered.
    """
    weights = weights or load_theme_weights()
    ordered = sorted(
        entries,
        key=lambda e: (
            -rank_value(e.get("score"), e.get("theme"), weights),
            -(e.get("score") or 0),
            (e.get("name") or "").lower(),          # stable, alphabetical last resort
        ),
    )
    assert len(ordered) == len(entries), "ranking must never drop an entry"
    return ordered
