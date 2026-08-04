"""
Re-deriving stored tier verdicts after a floor change.

The floor was narrowed on 2026-08-03 (it had been promoting 88% of the corpus to
WARM). Shipping that only changed signals scored afterwards — these tests cover
the job that fixes the ones already in the table, and the two things it must
never do: revisit a conviction HOT, or demote on a guess.
"""
import json

import pytest

from app.extensions import db as _db
from app.models.item import Item
from app.models.signal_event import SignalEvent
from app.services.confluence import normalize_brand
from app.services.enrichment import rederive_watch_levels


def _signal(owner_id, name, *, score, level, reason=None, **enr_extra):
    enr = {"enriched": True, "bullish_score": score, "watch_level": level}
    if reason is not None:
        enr["tier_floor_reason"] = reason
    enr.update(enr_extra)
    item = Item(title=name, item_type="signal", owner_id=owner_id,
            description=json.dumps({"company_name": name, "enrichment": enr}))
    _db.session.add(item)
    _db.session.flush()
    return item


def _events(owner_id, item_id, name, types):
    """Key events the way confluence really does — normalize_brand strips legal
    suffixes, so a hand-written key silently fails to join."""
    brand_key = normalize_brand(name)
    for t in types:
        _db.session.add(SignalEvent(item_id=item_id, owner_id=owner_id,
                               brand_key=brand_key, brand_name=brand_key,
                               signal_type=t))
    _db.session.flush()


def _level_of(item_id):
    row = _db.session.get(Item, item_id)
    return json.loads(row.description)["enrichment"]["watch_level"]


class TestRederive:
    def test_demotes_a_signal_the_old_floor_over_promoted(self, db, admin_user):
        """The whole point: score 22 is far under the 48 near-miss line."""
        it = _signal(admin_user.id, "copperfield mining", score=22,
                     level="warm", reason="trademark")
        _events(admin_user.id, it.id, "copperfield mining", ["trademark"])
        counts = rederive_watch_levels()
        assert _level_of(it.id) == "cold"
        assert counts["to_cold"] == 1

    def test_keeps_a_genuine_near_miss_warm(self, db, admin_user):
        it = _signal(admin_user.id, "nearmiss labs", score=52,
                     level="warm", reason="trademark")
        _events(admin_user.id, it.id, "nearmiss labs", ["trademark"])
        rederive_watch_levels()
        assert _level_of(it.id) == "warm"

    def test_triangulated_brand_stays_warm_below_the_near_miss_line(self, db, admin_user):
        """Trademark AND Form D outranks the model's read of a sparse filing."""
        it = _signal(admin_user.id, "twochannel co", score=30,
                     level="warm", reason="tm_plus_form_d")
        _events(admin_user.id, it.id, "twochannel co", ["trademark", "delaware"])
        rederive_watch_levels()
        assert _level_of(it.id) == "warm"

    def test_gate_failure_is_never_floored(self, db, admin_user):
        it = _signal(admin_user.id, "b2b saas inc", score=60, level="warm",
                     reason="trademark", consumer_brand=False)
        _events(admin_user.id, it.id, "b2b saas inc", ["trademark"])
        rederive_watch_levels()
        assert _level_of(it.id) == "cold"

    def test_conviction_hot_is_never_revisited(self, db, admin_user):
        """Invariant 2 — conviction outranks, and is not the floor's to reopen."""
        it = _signal(admin_user.id, "conviction brand", score=12,
                     level="hot", reason="conviction_match")
        _events(admin_user.id, it.id, "conviction brand", ["trademark"])
        counts = rederive_watch_levels()
        assert _level_of(it.id) == "hot"
        assert counts["conviction_skipped"] == 1

    def test_warm_with_no_recorded_reason_is_left_alone(self, db, admin_user):
        """No evidence a floor put it there, so demoting would be a guess."""
        it = _signal(admin_user.id, "organic warm", score=58, level="warm")
        _events(admin_user.id, it.id, "organic warm", ["trademark"])
        counts = rederive_watch_levels()
        assert _level_of(it.id) == "warm"
        assert counts["ambiguous"] == 1

    def test_model_hot_is_untouched(self, db, admin_user):
        it = _signal(admin_user.id, "real hot", score=81, level="hot")
        _events(admin_user.id, it.id, "real hot", ["trademark"])
        rederive_watch_levels()
        assert _level_of(it.id) == "hot"

    def test_dry_run_writes_nothing(self, db, admin_user):
        it = _signal(admin_user.id, "dryrun mining", score=22,
                     level="warm", reason="trademark")
        _events(admin_user.id, it.id, "dryrun mining", ["trademark"])
        _db.session.commit()
        counts = rederive_watch_levels(dry_run=True)
        assert counts["to_cold"] == 1 and counts["dry_run"] is True
        _db.session.expire_all()
        assert _level_of(it.id) == "warm", "dry run must not persist"

    def test_is_idempotent(self, db, admin_user):
        it = _signal(admin_user.id, "twice mining", score=22,
                     level="warm", reason="trademark")
        _events(admin_user.id, it.id, "twice mining", ["trademark"])
        rederive_watch_levels()
        second = rederive_watch_levels()
        assert _level_of(it.id) == "cold"
        assert second["changed"] == 0, "a second pass must be a no-op"

    def test_unscored_signals_are_skipped(self, db, admin_user):
        it = _signal(admin_user.id, "unscored", score=None, level="cold")
        counts = rederive_watch_levels()
        assert counts["examined"] == 0


class TestEndpoint:
    def test_requires_admin(self, client, user_token):
        r = client.post("/api/admin/rederive-tiers",
                    headers={"Authorization": f"Bearer {user_token}"})
        assert r.status_code in (401, 403)

    def test_returns_counts(self, client, admin_token):
        r = client.post("/api/admin/rederive-tiers", json={"dry_run": True},
                    headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200
        assert r.get_json()["dry_run"] is True
