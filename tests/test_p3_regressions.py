"""
Regressions found by the adversarial verification sweep of 2026-07-27.

Each of these was introduced by the audit remediation itself — the fixes were
right in intent and incomplete in reach.
"""
import json

from app.models.item import Item
from app.models.signal_event import SignalEvent
from app.services.confluence import record_signal_and_check_confluence
from app.services.scheduler import _fair_share

MONSTER = "THE MARK CONSISTS OF A STYLIZED CIRCULAR DESIGN " + "X" * 600


def test_over_long_brand_name_does_not_break_confluence(db, admin_user):
    """
    _safe_title fixed Item.title only. SignalEvent.brand_key and .brand_name are
    ALSO String(255) and were handed the untruncated name, so the DataError that
    killed every full scan simply moved into confluence — where it ALSO meant no
    SignalEvent was written, making that brand permanently invisible to
    triangulation.
    """
    item = Item(title=MONSTER[:255], owner_id=admin_user.id,
                item_type="signal", description="{}")
    db.session.add(item)
    db.session.flush()

    record_signal_and_check_confluence(
        item_id=item.id, owner_id=admin_user.id,
        brand_name=MONSTER, signal_type="trademark",
    )
    ev = SignalEvent.query.filter_by(item_id=item.id).first()
    assert ev is not None, "the signal must still be recorded for triangulation"
    assert len(ev.brand_name) <= 255
    assert len(ev.brand_key) <= 255


def test_form_d_is_not_starved_by_trademark_volume():
    """
    The budget was sliced off new_item_ids in COLLECTION order and the trademark
    sweep collects first, so a run saving ~3,000 trademarks against a 2,000
    budget scored zero Form D signals. Form D is Job 1, and an unscored signal is
    invisible to conviction matching, confluence, the watchlist and every alert.
    """
    ids = list(range(3000)) + list(range(3000, 3050))
    types = {i: ("trademark" if i < 3000 else "delaware") for i in ids}
    chosen = _fair_share(ids, types, 2000)

    assert len(chosen) == 2000
    form_d = [i for i in chosen if types[i] == "delaware"]
    assert len(form_d) == 50, "every Form D signal must be scored, not starved"


def test_fair_share_gives_unused_budget_back_to_the_big_source():
    """A small source must not waste budget it cannot use."""
    ids = list(range(100)) + [999]
    types = {i: ("trademark" if i < 100 else "delaware") for i in ids}
    chosen = _fair_share(ids, types, 50)
    assert len(chosen) == 50
    assert 999 in chosen, "the single Form D signal should be picked up early"


def test_fair_share_handles_empty_and_zero_budget():
    assert _fair_share([], {}, 100) == []
    assert _fair_share([1, 2], {1: "a", 2: "b"}, 0) == []
