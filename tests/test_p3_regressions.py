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


def test_alumni_match_does_not_block_a_conviction_founder():
    """
    Invariant 2: conviction outranks alumni.

    Stage (d) scans the signal's own text; stage (e) scans the Form D officers.
    The officer promotion was gated on `not conviction and not alumni`, so an
    alumni match found in the signal text BLOCKED a genuine conviction founder
    named among the officers — inverting the ranking and burying the strongest
    signal the product has behind the weaker one.
    """
    import inspect
    from app.services import signal_pipeline as sp

    src = inspect.getsource(sp.process_saved_signal)
    start = src.index("(e) Form D related persons")
    end = src.index("(f)", start)          # next stage marker, whatever it says
    stage_e = src[start:end]

    assert "if not conviction:" in stage_e, (
        "officer promotion must be gated on conviction alone — an existing "
        "alumni match must not block a conviction founder"
    )
    assert "if not conviction and not alumni:" not in stage_e
    # And a promoted conviction must displace the weaker designation.
    assert 'meta.pop("exit_alumni_match", None)' in stage_e


def test_scan_cooldown_cannot_eat_a_scheduled_night():
    """
    The cooldown is measured from when the last run ENDED. At 20h against a 24h
    cron, any run over 4 hours pushed the next night inside the window and that
    night was skipped entirely — and runs now routinely exceed 4h.
    """
    import inspect
    from app.services import scheduler as sch

    src = inspect.getsource(sch._run_all_scheduled)
    assert "cooldown_hours = 11" in src, "daily cooldown must be under half the 24h cron"
    assert "cooldown_hours = 20" not in src


def test_heartbeat_is_not_written_when_every_scan_was_skipped():
    """A heartbeat that advances on a no-op run is a lying instrument."""
    import inspect
    from app.services import scheduler as sch

    src = inspect.getsource(sch._run_all_scheduled)
    assert "if ran_any:" in src
    assert src.index("ran_any = True") < src.index("if ran_any:")
