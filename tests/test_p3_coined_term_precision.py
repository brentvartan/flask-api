"""
Coined-term confluence must merge legal-entity VARIANTS, not word neighbours.

Audited against 5,924 live production signals (2026-08-03): the mechanism was
producing 288 cross-brand merges and essentially none were real. The blocklist
approach cannot work — it was missing 'beauty' (30 distinct brands), 'wellness'
(16) and 'coffee' (16), in a consumer-brand tool.

The invariant says linking power may only grow. Its rationale says a wrong merge
is worse than a missed link. These merges were wrong: they inflate signal_count,
fabricate confluence hits, and email them as triangulated brands.
"""
import json

import pytest

from app.services.confluence import (
    _is_entity_variant,
    extract_coined_term_keys,
    normalize_brand,
)


class TestEntityVariantRule:
    @pytest.mark.parametrize("a,b", [
        ("olipop", "olipop beverages"),          # the case the feature exists for
        ("mosh", "mosh life foods"),
        ("filament", "filament sciences labs"),
    ])
    def test_legal_entity_variants_still_merge(self, a, b):
        assert _is_entity_variant(a, b)
        assert _is_entity_variant(b, a), "the rule is symmetric"

    @pytest.mark.parametrize("a,b", [
        ("advanced detox solutions", "h boisch solutions"),
        ("empirical security", "security plus systems"),
        ("simply clean", "simply salad restaurants"),
        ("burnout energy drink", "new energy freedom"),
        ("live mighty", "mighty spark"),
    ])
    def test_word_neighbours_do_not_merge(self, a, b):
        """Real pairs the engine was merging in production."""
        assert not _is_entity_variant(a, b)

    def test_identical_keys_are_not_a_variant_join(self):
        """Same key is the brand_key path's job, not the coined-term path's."""
        assert not _is_entity_variant("olipop", "olipop")

    def test_empty_key_never_merges(self):
        assert not _is_entity_variant("", "olipop")
        assert not _is_entity_variant("olipop", "")


class TestCategoryWordsAreNotCoinedTerms:
    @pytest.mark.parametrize("word", [
        "beauty", "wellness", "coffee", "solutions", "energy",
        "healthcare", "nutrition", "skincare", "organic", "premium",
    ])
    def test_consumer_category_words_are_blocked(self, word):
        """
        These are the words a consumer-brand tool sees most; every one of them
        was live in production as a 'distinctive coined term'.
        """
        assert extract_coined_term_keys(f"SOME {word.upper()} BRAND") == [] or \
            word not in extract_coined_term_keys(f"SOME {word.upper()} BRAND")

    def test_a_genuinely_coined_name_still_extracts(self):
        assert "olipop" in extract_coined_term_keys("OLIPOP BEVERAGES LLC")
        assert "filament" in extract_coined_term_keys("Filament Sciences")


class TestClusteringEndToEnd:
    def test_two_unrelated_brands_sharing_a_word_do_not_cluster(self, db, admin_user):
        from datetime import datetime, timezone
        from app.models.item import Item
        from app.models.signal_event import SignalEvent
        from app.services.confluence import _find_cluster

        a = Item(title="EMPIRICAL SECURITY", owner_id=admin_user.id,
                 item_type="signal", description="{}")
        db.session.add(a); db.session.flush()
        key_a = normalize_brand("EMPIRICAL SECURITY")
        db.session.add(SignalEvent(
            item_id=a.id, owner_id=admin_user.id, brand_key=key_a,
            brand_name="EMPIRICAL SECURITY", signal_type="trademark",
            detected_at=datetime.now(timezone.utc),
            person_keys=None,
            coined_term_keys=json.dumps(extract_coined_term_keys("EMPIRICAL SECURITY")),
        ))
        db.session.commit()

        key_b = normalize_brand("SECURITY PLUS SYSTEMS")
        _, _, cluster = _find_cluster(
            admin_user.id, key_b, [],
            extract_coined_term_keys("SECURITY PLUS SYSTEMS"),
        )
        assert cluster == [], "unrelated brands sharing 'security' must not cluster"

    def test_a_legal_entity_variant_still_clusters(self, db, admin_user):
        from datetime import datetime, timezone
        from app.models.item import Item
        from app.models.signal_event import SignalEvent
        from app.services.confluence import _find_cluster

        a = Item(title="OLIPOP", owner_id=admin_user.id, item_type="signal",
                 description="{}")
        db.session.add(a); db.session.flush()
        db.session.add(SignalEvent(
            item_id=a.id, owner_id=admin_user.id,
            brand_key=normalize_brand("OLIPOP"), brand_name="OLIPOP",
            signal_type="trademark", detected_at=datetime.now(timezone.utc),
            person_keys=None,
            coined_term_keys=json.dumps(extract_coined_term_keys("OLIPOP")),
        ))
        db.session.commit()

        key_b = normalize_brand("OLIPOP BEVERAGES LLC")
        _, _, cluster = _find_cluster(
            admin_user.id, key_b, [],
            extract_coined_term_keys("OLIPOP BEVERAGES LLC"),
        )
        assert len(cluster) == 1, "the OLIPOP case is exactly what this feature is for"
