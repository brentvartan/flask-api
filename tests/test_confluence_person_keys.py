"""
Task 2 tests — person key + coined-term key clustering in confluence.

All tests use the Flask test DB (via conftest fixtures). No live network calls.
"""
import json
import pytest

from app.services.confluence import (
    normalize_person,
    extract_person_keys,
    extract_coined_term_keys,
    record_signal_and_check_confluence,
)


# ── normalize_person ──────────────────────────────────────────────────────────

def test_normalize_person_basic():
    assert normalize_person("John Smith") == "john smith"


def test_normalize_person_last_first():
    assert normalize_person("Smith, John") == "john smith"


def test_normalize_person_none_on_na():
    assert normalize_person("N/A") is None
    assert normalize_person("N/A N/A") is None


def test_normalize_person_none_on_entity():
    assert normalize_person("Acme LLC") is None
    assert normalize_person("Sunrise Holdings Inc") is None


def test_normalize_person_none_on_digits():
    assert normalize_person("John Smith 2") is None


def test_normalize_person_none_on_single_word():
    assert normalize_person("Smith") is None


def test_normalize_person_middle_name():
    result = normalize_person("Mary Jane Watson")
    # Should use first and LAST token
    assert result == "mary watson"


# ── extract_person_keys ───────────────────────────────────────────────────────

def test_extract_person_keys_deduplicates():
    keys = extract_person_keys(["John Smith", "John Smith", "Jane Doe"])
    assert len(keys) == 2
    assert "john smith" in keys
    assert "jane doe" in keys


def test_extract_person_keys_filters_entities():
    keys = extract_person_keys(["Acme LLC", "John Smith", "N/A"])
    assert keys == ["john smith"]


def test_extract_person_keys_empty():
    assert extract_person_keys([]) == []
    assert extract_person_keys(None) == []


# ── extract_coined_term_keys ──────────────────────────────────────────────────

def test_extract_coined_term_keys_invented_word():
    keys = extract_coined_term_keys("Lightyear Incorporated")
    assert "lightyear" in keys


def test_extract_coined_term_keys_another_invented_word():
    keys = extract_coined_term_keys("Filament Sciences")
    assert "filament" in keys


def test_extract_coined_term_keys_strips_legal_suffix():
    keys = extract_coined_term_keys("Olipopx Beverages LLC")
    assert "olipopx" in keys
    assert "beverages" not in keys  # too common / stripped as suffix


def test_extract_coined_term_keys_rejects_common_words():
    # "fresh" and "clean" are in the common-word blocklist
    keys = extract_coined_term_keys("Fresh Clean Brands LLC")
    assert "fresh" not in keys
    assert "clean" not in keys


def test_extract_coined_term_keys_rejects_short_tokens():
    # Tokens < 5 chars are skipped
    keys = extract_coined_term_keys("HEY BRO")
    assert "hey" not in keys
    assert "bro" not in keys


def test_extract_coined_term_keys_empty():
    assert extract_coined_term_keys("") == []
    assert extract_coined_term_keys(None) == []


# ── Confluence clustering — person key ───────────────────────────────────────

def test_person_key_clusters_different_brand_keys(db, admin_user):
    """
    Form D "Lightyear Inc" (brand_key=lightyear) and
    Trademark "Filament Sciences" (brand_key=filament)
    both name "John Testfounder" → same person key → ONE confluence hit.
    """
    from app.models.item import Item

    item_a = Item(title="Lightyear", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="Filament Sciences", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    # Signal 1: Form D under "Lightyear"
    result_a = record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Lightyear Inc",
        signal_type="delaware",
        person_keys=["john testfounder"],
    )
    assert result_a["is_confluence"] is False  # first signal, no confluence yet

    # Signal 2: Trademark under "Filament Sciences" — same person, different brand name
    result_b = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Filament Sciences",
        signal_type="trademark",
        person_keys=["john testfounder"],
    )
    assert result_b["is_confluence"] is True
    assert result_b["signal_count"] == 2
    assert set(result_b["signal_types"]) == {"delaware", "trademark"}


def test_person_key_no_false_merge_different_people(db, admin_user):
    """Two signals with different person keys must NOT cluster."""
    from app.models.item import Item

    item_a = Item(title="BrandAlpha", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="BrandBeta", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="BrandAlpha",
        signal_type="delaware",
        person_keys=["alice founder"],
    )
    result = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="BrandBeta",
        signal_type="trademark",
        person_keys=["bob cofounder"],
    )
    assert result["is_confluence"] is False


# ── Confluence clustering — coined-term key ───────────────────────────────────

def test_coined_term_clusters_same_brand_different_entity_names(db, admin_user):
    """
    Trademark "POPZYX" (brand_key=popzyx) and
    Form D "POPZYX BEVERAGES LLC" (brand_key=popzyx beverages)
    → coined key "popzyx" links them → confluence hit.
    """
    from app.models.item import Item

    item_a = Item(title="Popzyx", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="Popzyx Beverages", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    result_a = record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Popzyx",
        signal_type="trademark",
        coined_term_keys=["popzyx"],
    )
    assert result_a["is_confluence"] is False

    result_b = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Popzyx Beverages LLC",
        signal_type="delaware",
        coined_term_keys=["popzyx"],
    )
    assert result_b["is_confluence"] is True
    assert result_b["signal_count"] == 2


def test_coined_term_no_false_merge_different_terms(db, admin_user):
    """Two signals with different coined terms must NOT cluster."""
    from app.models.item import Item

    item_a = Item(title="Zorbyx", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="Flibbix", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Zorbyx",
        signal_type="trademark",
        coined_term_keys=["zorbyx"],
    )
    result = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Flibbix",
        signal_type="delaware",
        coined_term_keys=["flibbix"],
    )
    assert result["is_confluence"] is False


# ── Existing brand_key confluence still works ─────────────────────────────────

def test_brand_key_confluence_unchanged(db, admin_user):
    """Original same-brand-key path continues to work."""
    from app.models.item import Item

    item_a = Item(title="Neuro Gum", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="Neuro Gum LLC", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    result_a = record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Neuro Gum",
        signal_type="trademark",
    )
    assert result_a["is_confluence"] is False

    result_b = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Neuro Gum LLC",
        signal_type="delaware",
    )
    assert result_b["is_confluence"] is True
    assert result_b["signal_count"] == 2


def test_same_signal_type_no_confluence(db, admin_user):
    """Two signals of the same type for the same brand must NOT fire confluence."""
    from app.models.item import Item

    item_a = Item(title="Moshx", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    item_b = Item(title="Moshx", owner_id=admin_user.id, item_type="signal",
                  description=json.dumps({"_type": "signal"}))
    db.session.add_all([item_a, item_b])
    db.session.commit()

    record_signal_and_check_confluence(
        item_id=item_a.id, owner_id=admin_user.id,
        brand_name="Moshx", signal_type="trademark",
    )
    result = record_signal_and_check_confluence(
        item_id=item_b.id, owner_id=admin_user.id,
        brand_name="Moshx", signal_type="trademark",
    )
    assert result["is_confluence"] is False
