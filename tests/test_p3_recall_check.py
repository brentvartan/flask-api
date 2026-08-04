"""
The recall half of the measurement.

Every other metric in this codebase answers "of what we surfaced, how much was
good?" — precision. This endpoint answers "of the deals that mattered, how many
did we have, and how early?", which nothing here could previously touch.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models.item import Item
from app.models.signal_event import SignalEvent
from app.services.confluence import normalize_brand


def _seen(owner_id, name, signal_type, when):
    item = Item(title=name, item_type="signal", owner_id=owner_id, description="{}")
    _db.session.add(item)
    _db.session.flush()
    ev = SignalEvent(item_id=item.id, owner_id=owner_id,
                     brand_key=normalize_brand(name), brand_name=name,
                     signal_type=signal_type, detected_at=when)
    _db.session.add(ev)
    _db.session.flush()
    return item


def _post(client, token, body):
    return client.post("/api/admin/recall-check", json=body,
                       headers={"Authorization": f"Bearer {token}"})


class TestRecallCheck:
    def test_reports_a_hit_with_its_earliest_sighting(self, client, db, admin_user, admin_token):
        early = datetime(2026, 1, 5, tzinfo=timezone.utc)
        _seen(admin_user.id, "Singing Pastures", "trademark", early)
        _seen(admin_user.id, "Singing Pastures", "delaware", early + timedelta(days=40))
        db.session.commit()

        r = _post(client, admin_token, {"brands": ["Singing Pastures"]})
        assert r.status_code == 200
        d = r.get_json()
        assert d["found"] == 1 and d["missed"] == 0 and d["recall_pct"] == 100.0
        res = d["results"][0]
        assert res["first_signal_type"] == "trademark", "earliest sighting, not latest"
        assert res["signal_types"] == ["delaware", "trademark"]
        assert res["signal_count"] == 2
        assert res["first_seen"].startswith("2026-01-05")

    def test_reports_a_miss(self, client, db, admin_user, admin_token):
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["Never Seen Co"]}).get_json()
        assert d["found"] == 0 and d["missed"] == 1 and d["recall_pct"] == 0.0
        assert d["results"][0]["found"] is False

    def test_matches_through_normalisation(self, client, db, admin_user, admin_token):
        """"Singing Pastures, LLC" and "SINGING PASTURES" are one brand
        everywhere else in this system; recall must not be the exception."""
        _seen(admin_user.id, "Singing Pastures", "trademark",
              datetime(2026, 2, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["SINGING PASTURES, LLC"]}).get_json()
        assert d["found"] == 1

    def test_lead_days_positive_when_the_signal_came_first(self, client, db, admin_user, admin_token):
        _seen(admin_user.id, "Early Brand", "trademark",
              datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {
            "brands": ["Early Brand"], "heard_at": {"Early Brand": "2026-04"},
        }).get_json()
        assert d["results"][0]["lead_days"] == 90, "Jan 1 -> Apr 1"

    def test_lead_days_negative_when_we_were_late(self, client, db, admin_user, admin_token):
        """A negative lead is the finding that matters — it says the human beat
        the system, and no amount of precision makes up for it."""
        _seen(admin_user.id, "Late Brand", "trademark",
              datetime(2026, 6, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {
            "brands": ["Late Brand"], "heard_at": {"Late Brand": "2026-03"},
        }).get_json()
        assert d["results"][0]["lead_days"] < 0

    def test_malformed_heard_at_does_not_kill_the_row(self, client, db, admin_user, admin_token):
        _seen(admin_user.id, "Fuzzy Brand", "trademark",
              datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {
            "brands": ["Fuzzy Brand"], "heard_at": {"Fuzzy Brand": "sometime in spring"},
        }).get_json()
        row = d["results"][0]
        assert row["found"] is True and "lead_days" not in row
        assert row["heard_at_error"] == "sometime in spring"

    def test_duplicate_names_collapse_to_one_row(self, client, db, admin_user, admin_token):
        _seen(admin_user.id, "Dup Brand", "trademark",
              datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token,
                  {"brands": ["Dup Brand", "DUP BRAND", "Dup Brand, Inc."]}).get_json()
        assert d["checked"] == 1, "one brand, not three"

    def test_rejects_empty_and_oversized_input(self, client, db, admin_user, admin_token):
        assert _post(client, admin_token, {"brands": []}).status_code == 400
        assert _post(client, admin_token,
                     {"brands": [f"b{i}" for i in range(1001)]}).status_code == 400

    def test_requires_admin(self, client, db, user_token):
        assert _post(client, user_token, {"brands": ["x"]}).status_code in (401, 403)


class TestTitleFallback:
    """Not every signal has a SignalEvent — confluence writes those, it post-dates
    part of the corpus and can fail on its own. Matching only on brand_key
    reports a MISS for signals demonstrably sitting in the table, and a recall
    number that is too low misleads exactly as much as one that is too high."""

    def test_finds_a_signal_that_has_no_signal_event(self, client, db, admin_user, admin_token):
        item = Item(title="ORPHAN BRAND", item_type="signal",
                    owner_id=admin_user.id, description="{}")
        db.session.add(item)
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["Orphan Brand"]}).get_json()
        assert d["found"] == 1, "brand_key-only matching would call this a miss"
        assert d["results"][0]["matched_by"] == "title"

    def test_event_and_title_do_not_double_count_as_two_brands(self, client, db, admin_user, admin_token):
        _seen(admin_user.id, "Both Ways", "trademark",
              datetime(2026, 3, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["Both Ways"]}).get_json()
        assert d["checked"] == 1 and d["found"] == 1

    def test_earliest_wins_across_both_match_paths(self, client, db, admin_user, admin_token):
        """A title-matched row older than any SignalEvent must move first_seen
        earlier — otherwise lead time is understated."""
        _seen(admin_user.id, "Time Test", "trademark",
              datetime(2026, 6, 1, tzinfo=timezone.utc))
        old = Item(title="TIME TEST", item_type="signal", owner_id=admin_user.id,
                   description="{}", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.session.add(old)
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["Time Test"]}).get_json()
        assert d["results"][0]["first_seen"].startswith("2026-01-01")

    def test_corpus_window_is_always_returned(self, client, db, admin_user, admin_token):
        """A recall figure is uninterpretable without knowing how far back the
        corpus actually reaches."""
        _seen(admin_user.id, "Window Brand", "trademark",
              datetime(2026, 5, 1, tzinfo=timezone.utc))
        db.session.commit()
        d = _post(client, admin_token, {"brands": ["Window Brand"]}).get_json()
        assert d["corpus_window"]["earliest_signal"] is not None
        assert d["corpus_window"]["latest_signal"] is not None
