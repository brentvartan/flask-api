"""
An over-long brand name must never be able to kill a scan.

Item.title is String(255). USPTO sometimes puts the DESIGN DESCRIPTION in the
wordmark field — "THE MARK CONSISTS OF A STYLIZED CIRCULAR DESIGN...", 572
characters observed live — and Postgres raises DataError on insert. Because
signals are inserted in batches, ONE such row failed the whole batch and ended
the run. Verified in production 2026-07-27: the scan reported
"StringDataRightTruncation: value too long for type character varying(255)".

This was invisible at the ~200 signals a run the save path was written for and
became certain at ~8,500.
"""
import json

from app.models.item import Item
from app.services.signal_pipeline import TITLE_MAX, _safe_title


def test_safe_title_truncates_to_the_column_width():
    assert len(_safe_title("X" * 600)) == TITLE_MAX


def test_safe_title_leaves_normal_names_untouched():
    assert _safe_title("Sunset Beverage") == "Sunset Beverage"


def test_safe_title_handles_none_and_empty():
    assert _safe_title(None) == ""
    assert _safe_title("") == ""


def test_an_over_long_name_can_be_inserted(db, admin_user):
    """The real regression: this insert used to raise and take the batch with it."""
    monster = "THE MARK CONSISTS OF A STYLIZED CIRCULAR DESIGN " + ("X" * 600)
    db.session.add(Item(
        title=_safe_title(monster),
        owner_id=admin_user.id,
        item_type="signal",
        description=json.dumps({"fp": "a" * 16, "company_name": monster}),
    ))
    db.session.commit()

    saved = Item.query.filter_by(item_type="signal").first()
    assert len(saved.title) <= TITLE_MAX
    # The full name survives in the description, which is Text — truncation is a
    # storage concession, not data loss.
    assert json.loads(saved.description)["company_name"] == monster


def test_one_bad_row_does_not_poison_a_batch(db, admin_user):
    """A batch containing an over-long name still commits every row."""
    rows = [
        Item(title=_safe_title(f"BRAND{i}"), owner_id=admin_user.id, item_type="signal",
             description=json.dumps({"fp": f"{i:016d}"}))
        for i in range(5)
    ]
    rows.append(Item(title=_safe_title("Z" * 900), owner_id=admin_user.id,
                     item_type="signal", description=json.dumps({"fp": "f" * 16})))
    db.session.add_all(rows)
    db.session.commit()
    assert Item.query.filter_by(item_type="signal").count() == 6
